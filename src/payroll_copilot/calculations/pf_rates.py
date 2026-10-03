"""Effective-dated PF rate rows -> the rate set in force on each part of a period (pure).

A rate set is everything PF needs on a day: contribution rates, the wage ceiling of each,
EPS age limit and the establishment admin minimums, with the rule version and sources.
The period is cut wherever ANY row starts or ends, so a future revision of a single rate
or ceiling needs only a new row - no code change.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from payroll_copilot.core.exceptions import RuleNotFoundError

ONE_DAY = timedelta(days=1)
REQUIRED = ("EE_PF", "ER_PF", "EPS", "EDLI", "EPF_ADMIN", "EPS_MAX_AGE")
OPTIONAL_DEFAULTS = {"EDLI_ADMIN": Decimal(0), "EPF_ADMIN_MIN": Decimal(0),
                     "EPF_ADMIN_MIN_NO_MEMBER": Decimal(0)}  # fmt: skip
MISSING_CONFIG = ("PF rule configuration for this payroll period is unavailable. "
                  "Payroll cannot be finalized.")  # fmt: skip


@dataclass(frozen=True)
class RateRow:
    rate_name: str
    rule_version: str
    rate_value: Decimal
    wage_ceiling: Decimal | None
    effective_from: date
    effective_to: date | None
    source: str
    status: str

    def covers(self, day: date) -> bool:
        ends_after = self.effective_to is None or day <= self.effective_to
        return self.effective_from <= day and ends_after


@dataclass(frozen=True)
class PFRateSet:
    start: date  # part of the requested period this set covers
    end: date
    version: str  # rule version of the PF wage ceiling row, e.g. "EPF v2"
    version_from: date  # when that version took effect (for explanations)
    ee: Decimal
    er: Decimal
    eps: Decimal
    edli: Decimal
    admin: Decimal
    edli_admin: Decimal
    pf_ceiling: Decimal
    eps_ceiling: Decimal
    edli_ceiling: Decimal
    eps_max_age: int
    admin_min: Decimal
    admin_min_no_member: Decimal
    status: str  # VERIFIED only if every row used is VERIFIED
    sources: tuple[str, ...]

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def validate_rows(rows: list[RateRow]) -> list[str]:
    """Problems that make the configuration unusable (overlaps, reversed ranges)."""
    problems = []
    by_name: dict[str, list[RateRow]] = {}
    for row in rows:
        if row.effective_to and row.effective_to < row.effective_from:
            problems.append(f"{row.rate_name} {row.effective_from}: effective_to before from")
        by_name.setdefault(row.rate_name, []).append(row)
    for name, items in by_name.items():
        items.sort(key=lambda r: r.effective_from)
        for a, b in zip(items, items[1:], strict=False):
            if a.effective_to is None or a.effective_to >= b.effective_from:
                problems.append(f"{name}: {a.rule_version} ({a.effective_from}) overlaps "
                                f"{b.rule_version} ({b.effective_from})")  # fmt: skip
    return problems


def change_dates(rows: list[RateRow], start: date, end: date) -> list[date]:
    """Dates inside (start, end] where any rate row starts or stops applying."""
    days = set()
    for row in rows:
        if start < row.effective_from <= end:
            days.add(row.effective_from)
        if row.effective_to and start <= row.effective_to < end:
            days.add(row.effective_to + ONE_DAY)
    return sorted(days)


def _set_on(rows: list[RateRow], day: date, end: date) -> PFRateSet:
    in_force: dict[str, RateRow] = {}
    for row in rows:
        if row.covers(day):
            if row.rate_name in in_force:
                raise RuleNotFoundError(f"Two {row.rate_name} rates apply on {day}: "
                                        f"{in_force[row.rate_name].rule_version} and "
                                        f"{row.rule_version}")  # fmt: skip
            in_force[row.rate_name] = row
    missing = [name for name in REQUIRED if name not in in_force]
    if missing:
        raise RuleNotFoundError(f"{MISSING_CONFIG} (no {', '.join(missing)} on {day})")

    def value(name: str) -> Decimal:
        return in_force[name].rate_value if name in in_force else OPTIONAL_DEFAULTS[name]

    def ceiling(name: str) -> Decimal:
        row = in_force[name]
        if row.wage_ceiling is None:
            raise RuleNotFoundError(f"{MISSING_CONFIG} ({name} has no wage ceiling on {day})")
        return row.wage_ceiling

    used = list(in_force.values())
    lead = in_force["EE_PF"]
    return PFRateSet(
        start=day, end=end, version=lead.rule_version, version_from=lead.effective_from,
        ee=value("EE_PF"), er=value("ER_PF"), eps=value("EPS"), edli=value("EDLI"),
        admin=value("EPF_ADMIN"), edli_admin=value("EDLI_ADMIN"),
        pf_ceiling=ceiling("EE_PF"), eps_ceiling=ceiling("EPS"), edli_ceiling=ceiling("EDLI"),
        eps_max_age=int(value("EPS_MAX_AGE")), admin_min=value("EPF_ADMIN_MIN"),
        admin_min_no_member=value("EPF_ADMIN_MIN_NO_MEMBER"),
        status="VERIFIED" if all(r.status == "VERIFIED" for r in used) else "UNVERIFIED",
        sources=tuple(dict.fromkeys(r.source for r in used)),
    )  # fmt: skip


def rate_sets(rows: list[RateRow], start: date, end: date) -> list[PFRateSet]:
    """Consecutive rate sets covering [start, end] exactly."""
    cuts = [start, *change_dates(rows, start, end), end + ONE_DAY]
    return [_set_on(rows, a, b - ONE_DAY) for a, b in zip(cuts, cuts[1:], strict=False)]


def rate_set_before(rows: list[RateRow], day: date) -> PFRateSet | None:
    """The rate set in force on the day before ``day`` (the previous version)."""
    try:
        return _set_on(rows, day - ONE_DAY, day - ONE_DAY)
    except RuleNotFoundError:
        return None
