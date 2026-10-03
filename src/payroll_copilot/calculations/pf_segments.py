"""Date-segmented PF engine (pure; no database).

    get_pf_rule_segments  cut the payroll month at every date where something relevant
                          changes: a statutory rate (from the rate rows - never hard-coded),
                          DOJ, PF applicable date, exit, salary revision, EPS membership,
                          the 58th birthday. Each segment has one rule version, one salary
                          and one EPS status.
    calculate_pf_segments earned PF wage per segment = monthly PF wage x paid days / divisor
                          (LOP by date). Ceilings are applied per RULE PERIOD and prorated by
                          that period's calendar days in the month (EPFO FAQ on S.O. 5109(E):
                          1-16 Sep on 15,000 x 16/30, 17-30 Sep on 25,000 x 14/30); with no rate
                          change in the month that is simply min(earned, ceiling). Segments are
                          added unrounded and each component is rounded once for the month.

Validated against EPFO's published illustrations (FAQ Q7 scenarios A/B/C, Q13 table) and
an independent reference implementation (tests/oracle).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from payroll_copilot.calculations.pf_rates import PFRateSet
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RUPEE, RoundingMode, round_money

ONE_DAY = timedelta(days=1)
ZERO = Decimal(0)
ENGINE_VERSION = "pf-segments-1"

STATUTORY_CEILING = "statutory_ceiling"
ACTUAL_WAGES = "actual_wages"
POLICIES = (STATUTORY_CEILING, ACTUAL_WAGES)
POLICY_ALIASES = {"ceiling_15000": STATUTORY_CEILING}  # old name from the spec


class PFDataError(CalculationError):
    """Employee data needed for PF is missing or inconsistent (never guessed)."""


@dataclass(frozen=True)
class SalaryPeriod:
    start: date
    end: date | None
    pf_wage: Decimal  # full-month PF wage (e.g. Basic + DA)


@dataclass(frozen=True)
class PFProfile:
    employee_code: str
    doj: date | None
    exit: date | None
    pf_applicable: bool
    pf_applicable_date: date | None
    dob: date
    eps_eligible: bool = True
    eps_member_from: date | None = None  # EPS starts on this date (overrides eps_eligible)
    policy: str = STATUTORY_CEILING
    exempt_reason: str | None = None


@dataclass(frozen=True)
class PFConfig:
    ceiling_scope: str = "RULE_PERIOD"  # or SEGMENT (prorate by the employee's own days)
    divisor: str = "CALENDAR"  # or FIXED_30
    edli_basis: str = "PF_WAGE"  # or CAPPED
    rounding: RoundingMode = RoundingMode.HALF_UP

    def as_dict(self) -> dict[str, str]:
        return {"ceiling_scope": self.ceiling_scope, "divisor": self.divisor,
                "edli_basis": self.edli_basis, "rounding": self.rounding.value}  # fmt: skip


@dataclass
class Segment:
    start: date
    end: date
    reason: str  # why this segment starts: "month start", "PF applicable date", ...
    rates: PFRateSet
    monthly_wage: Decimal
    eps_member: bool
    lop_days: Decimal = ZERO
    earned: Decimal = ZERO
    pf_wage: Decimal = ZERO
    eps_wage: Decimal = ZERO
    edli_wage: Decimal = ZERO

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_dict(self) -> dict[str, Any]:
        q = Decimal("0.01")
        return {
            "start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days,
            "reason": self.reason, "rule_version": self.rates.version,
            "rule_effective_from": self.rates.version_from.isoformat(),
            "rule_status": self.rates.status, "pf_ceiling": str(self.rates.pf_ceiling),
            "monthly_wage": str(self.monthly_wage), "lop_days": str(self.lop_days),
            "earned": str(self.earned.quantize(q)), "pf_wage": str(self.pf_wage.quantize(q)),
            "eps_wage": str(self.eps_wage.quantize(q)),
            "edli_wage": str(self.edli_wage.quantize(q)),
            "eps_member": self.eps_member,
            "employee_pf": str((self.pf_wage * self.rates.ee / 100).quantize(q)),
            "eps": str((self.eps_wage * self.rates.eps / 100).quantize(q)),
        }  # fmt: skip


@dataclass
class PFSegmentResult:
    status: str  # OK | NOT_APPLICABLE
    reason: str | None = None
    segments: list[Segment] = field(default_factory=list)
    amounts: dict[str, Decimal] = field(default_factory=dict)  # EE_PF ER_EPF EPS EDLI EPF_ADMIN
    pf_wage: Decimal = ZERO
    eps_wage: Decimal = ZERO
    edli_wage: Decimal = ZERO
    earned: Decimal = ZERO
    employer_total: Decimal = ZERO  # ER_EPF + EPS
    warnings: list[str] = field(default_factory=list)

    @property
    def rule_versions(self) -> list[str]:
        return list(dict.fromkeys(s.rates.version for s in self.segments))

    @property
    def employer_cost(self) -> Decimal:
        a = self.amounts
        return a.get("ER_EPF", ZERO) + a.get("EPS", ZERO) + a.get("EDLI", ZERO) + a.get(
            "EPF_ADMIN", ZERO)

    @property
    def pf_account_credit(self) -> Decimal:
        return self.amounts.get("EE_PF", ZERO) + self.amounts.get("ER_EPF", ZERO)


def normalise_policy(value: str | None) -> str:
    policy = POLICY_ALIASES.get(value or STATUTORY_CEILING, value or STATUTORY_CEILING)
    if policy not in POLICIES:
        raise PFDataError(f"Unknown PF wage policy {value!r} (use {' or '.join(POLICIES)})")
    return policy


def _age58(profile: PFProfile, max_age: int) -> date:
    dob = profile.dob
    try:
        return dob.replace(year=dob.year + max_age)
    except ValueError:  # 29 February
        return date(dob.year + max_age, 3, 1)


def _eps_member(profile: PFProfile, day: date, max_age: int) -> bool:
    if day >= _age58(profile, max_age):
        return False
    if profile.eps_member_from is not None:
        return day >= profile.eps_member_from
    return profile.eps_eligible


def _salary_on(salary: list[SalaryPeriod], day: date) -> SalaryPeriod | None:
    found = [s for s in salary if s.start <= day and (s.end is None or day <= s.end)]
    return found[-1] if found else None


def get_pf_rule_segments(
    profile: PFProfile, period: PayrollPeriod, rates: list[PFRateSet],
    salary: list[SalaryPeriod],
) -> tuple[list[Segment], str | None]:  # fmt: skip
    """Segments of the month, or ([], reason) when PF does not apply. Raises PFDataError."""
    if not profile.pf_applicable:
        return [], profile.exempt_reason or "PF is not applicable for this employee"
    if profile.doj is None:
        raise PFDataError("DOJ is missing for this employee. Please configure it before "
                          "processing payroll.")  # fmt: skip
    if profile.pf_applicable_date is None:
        raise PFDataError("PF Applicable Date is missing for this employee. Please configure "
                          "it before processing payroll.")  # fmt: skip
    if profile.pf_applicable_date < profile.doj:
        raise PFDataError(f"PF Applicable Date {profile.pf_applicable_date} is before DOJ "
                          f"{profile.doj}")  # fmt: skip
    if profile.exit and profile.exit < profile.doj:
        raise PFDataError(f"Exit date {profile.exit} is before DOJ {profile.doj}")

    start = max(period.start, profile.doj, profile.pf_applicable_date)
    end = min(period.end, profile.exit) if profile.exit else period.end
    if start > end:
        if start > period.end:
            return [], f"PF starts on {start.isoformat()}, after this month"
        return [], "Not employed in this month"

    reasons: dict[date, str] = {start: (
        "PF applicable date" if start == profile.pf_applicable_date and start > period.start
        else "DOJ" if start == profile.doj and start > period.start else "month start")}
    for rs in rates:
        if start < rs.start <= end:
            reasons.setdefault(rs.start, f"{rs.version} effective")
    for sp in salary:
        if start < sp.start <= end:
            reasons.setdefault(sp.start, "salary revision")
    max_age = rates[0].eps_max_age if rates else 58
    if profile.eps_member_from and start < profile.eps_member_from <= end:
        reasons.setdefault(profile.eps_member_from, "EPS membership starts")
    birthday = _age58(profile, max_age)
    if start < birthday <= end:
        reasons.setdefault(birthday, f"age {max_age} (EPS stops)")

    cuts = sorted(reasons)
    segments = []
    for i, seg_start in enumerate(cuts):
        seg_end = (cuts[i + 1] - ONE_DAY) if i + 1 < len(cuts) else end
        rs = next((r for r in rates if r.start <= seg_start <= r.end), None)
        if rs is None:
            from payroll_copilot.calculations.pf_rates import MISSING_CONFIG

            raise PFDataError(f"{MISSING_CONFIG} (no rates for {seg_start})")
        sp = _salary_on(salary, seg_start)
        if sp is None:
            raise PFDataError(f"No salary structure for {seg_start.isoformat()} to "
                              f"{seg_end.isoformat()}")  # fmt: skip
        segments.append(Segment(seg_start, seg_end, reasons[seg_start], rs, sp.pf_wage,
                                _eps_member(profile, seg_start, rs.eps_max_age)))  # fmt: skip
    return segments, None


def calculate_pf_segments(
    profile: PFProfile, period: PayrollPeriod, rates: list[PFRateSet],
    salary: list[SalaryPeriod], lop: dict[date, Decimal] | None = None,
    config: PFConfig | None = None,
) -> PFSegmentResult:  # fmt: skip
    config = config or PFConfig()
    segments, not_applicable = get_pf_rule_segments(profile, period, rates, salary)
    if not_applicable:
        return PFSegmentResult(status="NOT_APPLICABLE", reason=not_applicable)
    policy = normalise_policy(profile.policy)
    lop = lop or {}
    divisor = Decimal(period.calendar_days if config.divisor == "CALENDAR" else 30)
    warnings = []
    outside = [d for d in lop if not any(s.start <= d <= s.end for s in segments)
               and period.start <= d <= period.end]  # fmt: skip
    if outside:
        warnings.append(f"LOP on {', '.join(d.isoformat() for d in sorted(outside))} is outside "
                        "the PF-applicable days and was ignored for PF")  # fmt: skip

    for s in segments:
        s.lop_days = sum((v for d, v in lop.items() if s.start <= d <= s.end), ZERO)
        if s.lop_days > s.days:
            raise PFDataError(f"LOP {s.lop_days} days exceeds {s.days} days in {s.start}..{s.end}")
        s.earned = s.monthly_wage * (s.days - s.lop_days) / divisor

    # group segments into caps: one cap per rule period (or per segment when configured)
    groups: dict[Any, list[Segment]] = {}
    for s in segments:
        key = id(s) if config.ceiling_scope == "SEGMENT" else (s.rates.start, s.rates.end)
        groups.setdefault(key, []).append(s)
    for members in groups.values():
        rs = members[0].rates
        cap_days = (sum(m.days for m in members) if config.ceiling_scope == "SEGMENT"
                    else rs.days)  # fmt: skip
        earned = sum((m.earned for m in members), ZERO)
        eps_earned = sum((m.earned for m in members if m.eps_member), ZERO)
        pf_cap = rs.pf_ceiling * cap_days / divisor
        pf_total = earned if policy == ACTUAL_WAGES else min(earned, pf_cap)
        eps_total = min(eps_earned, rs.eps_ceiling * cap_days / divisor)
        edli_total = (pf_total if config.edli_basis == "PF_WAGE"
                      else min(earned, rs.edli_ceiling * cap_days / divisor))  # fmt: skip
        for m in members:  # spread capped totals over the members (display and audit)
            share = m.earned / earned if earned else ZERO
            eps_share = m.earned / eps_earned if eps_earned and m.eps_member else ZERO
            m.pf_wage, m.edli_wage = pf_total * share, edli_total * share
            m.eps_wage = eps_total * eps_share

    def rnd(value: Decimal) -> Decimal:
        return round_money(value, config.rounding, RUPEE)

    ee = rnd(sum((s.pf_wage * s.rates.ee / 100 for s in segments), ZERO))
    er = rnd(sum((s.pf_wage * s.rates.er / 100 for s in segments), ZERO))
    eps = rnd(sum((s.eps_wage * s.rates.eps / 100 for s in segments), ZERO))
    edli = rnd(sum((s.edli_wage * s.rates.edli / 100 for s in segments), ZERO))
    admin = rnd(sum((s.pf_wage * s.rates.admin / 100 for s in segments), ZERO))
    if any(s.rates.status != "VERIFIED" for s in segments):
        warnings.append("PF uses UNVERIFIED statutory rates - verify before finalizing")
    q = Decimal("0.01")
    return PFSegmentResult(
        status="OK", segments=segments,
        amounts={"EE_PF": ee, "ER_EPF": er - eps, "EPS": eps, "EDLI": edli, "EPF_ADMIN": admin},
        pf_wage=sum((s.pf_wage for s in segments), ZERO).quantize(q),
        eps_wage=sum((s.eps_wage for s in segments), ZERO).quantize(q),
        edli_wage=sum((s.edli_wage for s in segments), ZERO).quantize(q),
        earned=sum((s.earned for s in segments), ZERO).quantize(q),
        employer_total=er, warnings=warnings,
    )  # fmt: skip
