"""Independent reference ("oracle") PF calculator for the test matrix.

Implements the EPFO FAQ method for S.O. 5109(E) (ceiling 15,000 -> 25,000 w.e.f. 17-09-2026):
split the wage month at every date where something changes, prorate wages AND ceilings by
segment days / divisor, compute each segment, add up, round once per component.
Deliberately written separately from the app engine so it can serve as a test oracle.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP
from decimal import Decimal as D

ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class RuleVersion:  # one effective-dated set of statutory PF rates
    name: str
    start: date
    end: date | None
    pf_ceiling: D
    eps_ceiling: D
    edli_ceiling: D
    ee_rate: D = D("12")
    er_rate: D = D("12")
    eps_rate: D = D("8.33")
    edli_rate: D = D("0.5")
    admin_rate: D = D("0.5")


OLD = RuleVersion("OLD (EPF v1, ceiling 15,000)", date(2014, 9, 1), date(2026, 9, 16),
                  D(15000), D(15000), D(15000))
NEW = RuleVersion("NEW (EPF v2, ceiling 25,000, S.O. 5109(E))", date(2026, 9, 17), None,
                  D(25000), D(25000), D(25000))
RULES = [OLD, NEW]


@dataclass
class Employee:
    wage: D  # monthly PF wage (Basic+DA or statutory wages) at month start
    doj: date | None = date(2020, 1, 1)
    pf_from: date | None = date(2020, 1, 1)  # PF applicable date; None = PF not applicable
    exit: date | None = None
    lop: tuple[date, ...] = ()
    revisions: tuple[tuple[date, D], ...] = ()  # (effective date, new monthly PF wage)
    policy: str = "statutory_ceiling"  # or "actual_wages"
    eps: str = "member"  # member | epf_only (joins EPS on rule date if wage <= new ceiling) | none
    dob: date = date(1990, 1, 1)


@dataclass
class Segment:
    start: date
    end: date
    rule: RuleVersion
    wage: D
    days: int
    lop: int
    pf_wage: D
    eps_wage: D
    edli_wage: D


@dataclass
class Result:
    status: str
    reason: str
    segments: list[Segment] = field(default_factory=list)
    pf_wage: D = D(0)
    ee: int = 0
    er_epf: int = 0
    eps: int = 0
    edli: int = 0
    admin: int = 0

    @property
    def er_total(self) -> int:
        return self.er_epf + self.eps

    @property
    def pf_credit(self) -> int:  # what lands in the member's EPF account
        return self.ee + self.er_epf

    @property
    def employer_cost(self) -> int:
        return self.er_epf + self.eps + self.edli + self.admin


def r(x: D) -> int:
    return int(x.quantize(D(1), rounding=ROUND_HALF_UP))


def rule_on(day: date, rules: list[RuleVersion]) -> RuleVersion | None:
    for rv in rules:
        if rv.start <= day and (rv.end is None or day <= rv.end):
            return rv
    return None


def wage_on(emp: Employee, day: date) -> D:
    wage = emp.wage
    for eff, new in sorted(emp.revisions):
        if eff <= day:
            wage = new
    return wage


def eps_member_on(emp: Employee, day: date, rules: list[RuleVersion]) -> bool:
    if emp.eps == "none":
        return False
    age58 = date(emp.dob.year + 58, emp.dob.month, emp.dob.day)
    if day >= age58:
        return False
    if emp.eps == "member":
        return True
    # epf_only: becomes an EPS member from the effective date of a rule whose EPS ceiling
    # covers the wage on that date (EPFO FAQ Q9/Q12/Q13 footnote)
    for rv in rules:
        if rv.start <= day and wage_on(emp, rv.start) <= rv.eps_ceiling and rv is not rules[0]:
            return True
    return False


def calculate(emp: Employee, month_start: date, month_end: date,
              rules: list[RuleVersion] = RULES, divisor: str = "calendar",
              edli_basis: str = "pf_wage") -> Result:
    if emp.pf_from is None:
        return Result("NOT_APPLICABLE", "PF not applicable (no PF applicable date)")
    if emp.doj is None:
        return Result("ERROR", "DOJ missing")
    if emp.pf_from < emp.doj:
        return Result("ERROR", "PF applicable date is before DOJ")
    if emp.exit and emp.exit < emp.doj:
        return Result("ERROR", "Exit date is before DOJ")
    start = max(month_start, emp.doj, emp.pf_from)
    end = min(month_end, emp.exit) if emp.exit else month_end
    if start > end:
        if emp.doj > month_end or emp.pf_from > month_end:
            return Result("NOT_APPLICABLE", "PF not applicable in this month (starts later)")
        return Result("NOT_APPLICABLE", "Not employed / PF-applicable in this month")
    dv = D((month_end - month_start).days + 1) if divisor == "calendar" else D(30)

    # boundaries: every date where rule, wage, or EPS membership can change
    cuts = {start, end + ONE_DAY}
    cuts |= {rv.start for rv in rules if start < rv.start <= end}
    cuts |= {eff for eff, _ in emp.revisions if start < eff <= end}
    age58 = date(emp.dob.year + 58, emp.dob.month, emp.dob.day)
    if start < age58 <= end:
        cuts.add(age58)
    edges = sorted(cuts)
    res = Result("OK", "")
    for a, b in zip(edges, edges[1:], strict=False):
        seg_end = b - ONE_DAY
        rv = rule_on(a, rules)
        if rv is None:
            return Result("ERROR", f"PF rule configuration missing for {a}")
        days = (seg_end - a).days + 1
        lop = sum(1 for d in emp.lop if a <= d <= seg_end)
        wage = wage_on(emp, a)
        earned = wage * (days - lop) / dv
        member = eps_member_on(emp, a, rules)
        # caps are applied per RULE PERIOD below; a segment only carries its earned wage
        res.segments.append(Segment(a, seg_end, rv, wage, days, lop, earned,
                                    earned if member else D(0), D(0)))
    # ceilings: prorated by the rule period's calendar days in the month (same for every
    # employee); joining / exit / LOP / revisions only reduce the earned wage
    for rv in {s.rule.name: s.rule for s in res.segments}.values():
        segs = [s for s in res.segments if s.rule is rv]
        r_start, r_end = max(rv.start, month_start), min(rv.end or month_end, month_end)
        r_days = D((r_end - r_start).days + 1)
        earned = sum(s.pf_wage for s in segs)
        eps_earned = sum(s.eps_wage for s in segs)
        pf_cap, eps_cap = rv.pf_ceiling * r_days / dv, rv.eps_ceiling * r_days / dv
        pf_total = earned if emp.policy == "actual_wages" else min(earned, pf_cap)
        eps_total = min(eps_earned, eps_cap)
        edli_total = pf_total if edli_basis == "pf_wage" else min(earned, rv.edli_ceiling * r_days / dv)
        # spread the capped totals back over the segments pro rata (for display/audit only)
        for s in segs:
            share = s.pf_wage / earned if earned else D(0)
            eps_share = s.eps_wage / eps_earned if eps_earned else D(0)
            s.pf_wage, s.eps_wage, s.edli_wage = pf_total * share, eps_total * eps_share, edli_total * share
    # combine unrounded segment amounts, round once per component (EPFO FAQ Q7 method)
    ee = sum(s.pf_wage * s.rule.ee_rate / 100 for s in res.segments)
    er = sum(s.pf_wage * s.rule.er_rate / 100 for s in res.segments)
    eps = sum(s.eps_wage * s.rule.eps_rate / 100 for s in res.segments)
    edli = sum(s.edli_wage * s.rule.edli_rate / 100 for s in res.segments)
    admin = sum(s.pf_wage * s.rule.admin_rate / 100 for s in res.segments)
    res.pf_wage = sum(s.pf_wage for s in res.segments).quantize(D("0.01"), ROUND_HALF_UP)
    res.ee, res.eps, res.edli, res.admin = r(ee), r(eps), r(edli), r(admin)
    res.er_epf = r(er) - res.eps
    res.raw = {"ee": ee, "er_epf": er - eps, "eps": eps, "edli": edli, "admin": admin}
    return res


def old_only(emp: Employee, month_start: date, month_end: date) -> Result:
    """Counterfactual baseline: the whole month under the OLD rule only."""
    old = RuleVersion(OLD.name, OLD.start, None, OLD.pf_ceiling, OLD.eps_ceiling, OLD.edli_ceiling)
    return calculate(emp, month_start, month_end, [old])
