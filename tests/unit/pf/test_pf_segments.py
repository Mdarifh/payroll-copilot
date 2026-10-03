"""Date-segmented PF engine vs EPFO's published illustrations and the independent oracle."""

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from payroll_copilot.calculations.pf_rates import RateRow, rate_set_before, rate_sets, validate_rows
from payroll_copilot.calculations.pf_segments import (
    PFConfig,
    PFDataError,
    PFProfile,
    SalaryPeriod,
    calculate_pf_segments,
    get_pf_rule_segments,
)
from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.rules.loader import read_yaml
from tests.oracle import pf_oracle
from tests.oracle.matrix_cases import CASES

RATES_FILE = PROJECT_ROOT / "config" / "statutory" / "pf_rates.yaml"
SEP, OCT, AUG = PayrollPeriod(2026, 9), PayrollPeriod(2026, 10), PayrollPeriod(2026, 8)
NEW_RULE_DATE = date(2026, 9, 17)


def load_rows(path: Path = RATES_FILE) -> list[RateRow]:
    data = read_yaml(path)
    return [RateRow(rate_name=r["rate_name"], rule_version=r["rule_version"],
                    rate_value=D(str(r["rate_value"])),
                    wage_ceiling=None if r["wage_ceiling"] is None else D(str(r["wage_ceiling"])),
                    effective_from=r["effective_from"], effective_to=r["effective_to"],
                    source=data["sources"][r["source"]], status=r["status"])
            for r in data["rates"]]  # fmt: skip


ROWS = load_rows()


def run_engine(emp: pf_oracle.Employee, period: PayrollPeriod, edli="pf_wage"):
    """Translate an oracle Employee into engine inputs and calculate."""
    eps_from, eligible = None, emp.eps == "member"
    if emp.eps == "epf_only":  # HR moves EPF-only members earning <= 25,000 to EPS on 17-Sep
        wage_on_rule_date = pf_oracle.wage_on(emp, NEW_RULE_DATE)
        eps_from = NEW_RULE_DATE if wage_on_rule_date <= 25000 else None
    profile = PFProfile(
        employee_code="T", doj=emp.doj, exit=emp.exit, pf_applicable=emp.pf_from is not None,
        pf_applicable_date=emp.pf_from, dob=emp.dob, eps_eligible=eligible,
        eps_member_from=eps_from, policy=emp.policy,
    )  # fmt: skip
    timeline = [(date(2000, 1, 1), emp.wage), *sorted(emp.revisions)]
    salary = [SalaryPeriod(start, None, wage) for start, wage in timeline]
    lop = {day: D(1) for day in emp.lop}
    config = PFConfig(edli_basis="PF_WAGE" if edli == "pf_wage" else "CAPPED")
    return calculate_pf_segments(profile, period, rate_sets(ROWS, period.start, period.end),
                                 salary, lop, config)  # fmt: skip


# ---------------------------------------------------------------------- rate configuration


def test_rate_file_is_valid_and_effective_17_september():
    assert validate_rows(ROWS) == []
    sets = rate_sets(ROWS, SEP.start, SEP.end)
    assert [(s.start.day, s.end.day, s.version, s.pf_ceiling) for s in sets] == [
        (1, 16, "EPF v1", 15000), (17, 30, "EPF v2", 25000)]
    assert all(s.status == "VERIFIED" for s in sets)
    assert "S.O. 5109(E)" in " ".join(sets[1].sources)
    assert [s.version for s in rate_sets(ROWS, AUG.start, AUG.end)] == ["EPF v1"]
    assert [s.version for s in rate_sets(ROWS, OCT.start, OCT.end)] == ["EPF v2"]
    assert rate_set_before(ROWS, NEW_RULE_DATE).version == "EPF v1"


def test_missing_and_overlapping_configuration_is_an_error():
    from payroll_copilot.core.exceptions import RuleNotFoundError

    no_v2 = [r for r in ROWS if r.rule_version != "EPF v2"]
    with pytest.raises(RuleNotFoundError, match="PF rule configuration for this payroll period"):
        rate_sets(no_v2, SEP.start, SEP.end)
    clash = [*ROWS, RateRow("EE_PF", "EPF v9", D(10), D(25000), date(2026, 9, 20), None, "x",
                            "UNVERIFIED")]  # fmt: skip
    assert validate_rows(clash)
    with pytest.raises(RuleNotFoundError, match="Two EE_PF rates"):
        rate_sets(clash, SEP.start, SEP.end)


def test_future_revision_needs_only_a_new_row():
    """A made-up 1-Jan-2028 ceiling of 30,000 splits January without code changes."""
    future = [RateRow(r.rate_name, r.rule_version, r.rate_value, r.wage_ceiling, r.effective_from,
                      date(2028, 1, 9) if r.rule_version == "EPF v2" else r.effective_to,
                      r.source, r.status) for r in ROWS]  # fmt: skip
    future += [RateRow(n, "EPF v3", v, D(30000) if c else None, date(2028, 1, 10), None, "test",
                       "UNVERIFIED")
               for n, v, c in (("EE_PF", D(12), 1), ("ER_PF", D(12), 1), ("EPS", D("8.33"), 1),
                               ("EDLI", D("0.5"), 1), ("EPF_ADMIN", D("0.5"), 0))]  # fmt: skip
    jan = PayrollPeriod(2028, 1)
    sets = rate_sets(future, jan.start, jan.end)
    assert [(s.start.day, s.version) for s in sets] == [(1, "EPF v2"), (10, "EPF v3")]


# ---------------------------------------------------------------------- oracle vs EPFO


@pytest.mark.parametrize(("emp", "expected"), [
    # EPFO FAQ Q7 (S.O. 5109(E) transition, employee earning 20,000), amounts in paise
    (pf_oracle.Employee(D(20000), pf_from=date(2026, 9, 17)),
     {"ee": "1120.00", "er_epf": "342.53", "eps": "777.47", "edli": "46.67", "admin": "46.67"}),
    (pf_oracle.Employee(D(20000), policy="actual_wages", eps="epf_only"),
     {"ee": "2400.00", "er_epf": "1622.53", "eps": "777.47", "edli": "100.00", "admin": "100.00"}),
    (pf_oracle.Employee(D(20000)),
     {"ee": "2080.00", "er_epf": "636.13", "eps": "1443.87", "edli": "86.67", "admin": "86.67"}),
], ids=["Q7-A", "Q7-B", "Q7-C"])  # fmt: skip
def test_oracle_matches_epfo_faq(emp, expected):
    res = pf_oracle.calculate(emp, SEP.start, SEP.end)
    assert {k: f"{v:.2f}" for k, v in res.raw.items()} == expected


@pytest.mark.parametrize(("wage", "row"), [
    (10000, (1200, 833, 367, 50, 50)), (15000, (1800, 1250, 550, 75, 75)),
    (20000, (2400, 1666, 734, 100, 100)), (25000, (3000, 2083, 917, 125, 125)),
    (35000, (3000, 0, 3000, 125, 125)),
])  # fmt: skip
def test_engine_matches_epfo_faq_q13_table(wage, row):
    emp = pf_oracle.Employee(D(wage), eps="member" if wage <= 25000 else "none")
    a = run_engine(emp, OCT).amounts
    assert (a["EE_PF"], a["EPS"], a["ER_EPF"], a["EDLI"], a["EPF_ADMIN"]) == row


# ---------------------------------------------------------------------- full matrix


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_engine_equals_oracle_for_every_matrix_case(case):
    cid, _group, _desc, emp, month, edli = case
    period = PayrollPeriod.parse(month)
    expected = pf_oracle.calculate(emp, period.start, period.end, edli_basis=edli)
    if expected.status == "ERROR":
        with pytest.raises(PFDataError):
            run_engine(emp, period, edli)
        return
    got = run_engine(emp, period, edli)
    assert got.status == expected.status, cid
    if expected.status != "OK":
        return
    assert [(s.start, s.end, s.rates.version, s.monthly_wage) for s in got.segments] == [
        (s.start, s.end, s.rule.name.split(" ")[0].replace("OLD", "EPF v1").replace(
            "NEW", "EPF v2"), s.wage) for s in expected.segments]  # fmt: skip
    a = got.amounts
    assert (got.pf_wage, a["EE_PF"], a["ER_EPF"], a["EPS"], a["EDLI"], a["EPF_ADMIN"]) == (
        expected.pf_wage, expected.ee, expected.er_epf, expected.eps, expected.edli,
        expected.admin), cid


# ---------------------------------------------------------------------- spec examples, explicit


def segs(emp, period=SEP):
    return [(s.start.day, s.end.day, s.rates.version) for s in run_engine(emp, period).segments]


def test_spec_examples_segments():
    E = pf_oracle.Employee  # noqa: N806
    assert segs(E(D(30000), date(2026, 8, 10), date(2026, 8, 10))) == [
        (1, 16, "EPF v1"), (17, 30, "EPF v2")]  # CASE A
    assert segs(E(D(30000), date(2026, 9, 17), date(2026, 9, 17))) == [(17, 30, "EPF v2")]  # C
    assert segs(E(D(30000), date(2026, 9, 25), date(2026, 9, 25))) == [(25, 30, "EPF v2")]  # D
    assert segs(E(D(30000), date(2026, 8, 1), date(2026, 9, 20))) == [(20, 30, "EPF v2")]  # E
    assert segs(E(D(30000), exit=date(2026, 9, 20))) == [(1, 16, "EPF v1"), (17, 20, "EPF v2")]
    revision = E(D(20000), revisions=((date(2026, 9, 20), D(30000)),))  # section 17
    assert segs(revision) == [(1, 16, "EPF v1"), (17, 19, "EPF v2"), (20, 30, "EPF v2")]


def test_lop_is_applied_inside_each_segment():
    emp = pf_oracle.Employee(D(15000), lop=(date(2026, 9, 15), date(2026, 9, 16),
                                            date(2026, 9, 17)))  # fmt: skip
    res = run_engine(emp, SEP)
    assert [(s.lop_days, f"{s.earned:.2f}") for s in res.segments] == [
        (2, "7000.00"), (1, "6500.00")]
    assert res.pf_wage == D("13500.00") and res.amounts["EE_PF"] == 1620


def test_not_applicable_and_data_errors():
    E = pf_oracle.Employee  # noqa: N806
    intern = run_engine(E(D(10000), pf_from=None), SEP)
    assert intern.status == "NOT_APPLICABLE" and intern.amounts == {}
    with pytest.raises(PFDataError, match="PF Applicable Date is missing"):
        profile = PFProfile("X", date(2020, 1, 1), None, True, None, date(1990, 1, 1))
        get_pf_rule_segments(profile, SEP, rate_sets(ROWS, SEP.start, SEP.end),
                             [SalaryPeriod(date(2020, 1, 1), None, D(1))])  # fmt: skip
    with pytest.raises(PFDataError, match="DOJ is missing"):
        profile = PFProfile("X", None, None, True, date(2020, 1, 1), date(1990, 1, 1))
        get_pf_rule_segments(profile, SEP, rate_sets(ROWS, SEP.start, SEP.end), [])
    with pytest.raises(PFDataError, match="No salary structure"):
        profile = PFProfile("X", date(2020, 1, 1), None, True, date(2020, 1, 1), date(1990, 1, 1))
        get_pf_rule_segments(profile, SEP, rate_sets(ROWS, SEP.start, SEP.end), [])


def test_segments_partition_the_pf_days():
    """Property: segments never overlap and cover exactly the PF-applicable days."""
    for cid, _g, _d, emp, month, edli in CASES:
        period = PayrollPeriod.parse(month)
        try:
            res = run_engine(emp, period, edli)
        except PFDataError:
            continue
        if res.status != "OK":
            continue
        days = [s.days for s in res.segments]
        assert all(a.end < b.start for a, b in zip(res.segments, res.segments[1:], strict=False)), cid
        start = max(period.start, emp.doj, emp.pf_from)
        end = min(period.end, emp.exit) if emp.exit else period.end
        assert sum(days) == (end - start).days + 1, cid
