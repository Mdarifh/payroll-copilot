from decimal import Decimal

import pytest

from payroll_copilot.calculations.root_cause import explain_net, is_rounding, matching_candidates
from payroll_copilot.calculations.validation import (
    ComponentGroup,
    EmployeeValidation,
    IssueType,
    compare,
)
from payroll_copilot.domain.enums import ComparisonStatus as S

D = Decimal
G = ComponentGroup


THREE_K = D(3000)


def c(component="EE_PF", expected=THREE_K, actual=THREE_K, group=G.PF, **kw):
    return compare("E1", "2026-09", component, group, expected, actual, **kw)


class TestCompare:
    @pytest.mark.parametrize(
        ("expected", "actual", "tol", "status", "diff"),
        [
            (D(3000), D(3000), D(0), S.MATCH, D(0)),
            (D(3000), D(2700), D(0), S.MISMATCH, D(-300)),
            (D(144), D(143), D(1), S.WITHIN_TOLERANCE, D(-1)),
            (D(144), D(142), D(1), S.MISMATCH, D(-2)),
            (D(200), None, D(0), S.MISSING_ACTUAL, None),
            (None, D(500), D(0), S.MISSING_EXPECTED, None),
            (D(0), None, D(0), S.MATCH, D(0)),
            (None, D(0), D(0), S.MATCH, D(0)),
            (None, None, D(0), S.MATCH, None),
        ],
    )
    def test_statuses(self, expected, actual, tol, status, diff):
        r = c(expected=expected, actual=actual, tolerance=tol)
        assert (r.status, r.difference) == (status, diff)

    def test_days_ignore_tolerance(self):
        r = c("PAID_DAYS", D(27), D(28), G.ATTENDANCE, tolerance=D(5), unit="DAYS")
        assert r.status is S.MISMATCH and r.tolerance == 0
        assert r.fmt(r.difference) == "1 days"

    def test_is_issue(self):
        assert c(actual=D(1)).is_issue
        assert not c(actual=D(2999), tolerance=D(1)).is_issue

    def test_diagnosis_and_text(self):
        r = c(actual=D(1800)).with_diagnosis(IssueType.RULE_VERSION, ["old ceiling"])
        v = EmployeeValidation(employee_code="E1", employee_name="X", period="2026-09",
                               results=[r, c()])  # fmt: skip
        assert v.issues == [r]
        assert v.status_counts == {"MISMATCH": 1, "MATCH": 1}
        text = v.to_text(only_issues=True)
        assert "EE_PF" in text and "-₹1,200" in text and "[RULE_VERSION] old ceiling" in text
        assert text.count("EE_PF") == 1


class TestRootCauseHelpers:
    def test_is_rounding(self):
        assert is_rounding(D(-1)) and is_rounding(D("0.5"))
        assert not is_rounding(D(0)) and not is_rounding(D(2)) and not is_rounding(None)

    def test_matching_candidates(self):
        found = matching_candidates(D(1800), {"old rule": D(1800), "actual wage": D(3600)})
        assert found == ["old rule"]
        assert matching_candidates(None, {"x": D(1)}) == []
        assert matching_candidates(D(4001), {"lop": D(4000)}, tolerance=D(3)) == ["lop"]

    def test_explain_net_reconciles(self):
        results = [
            c("GROSS", D(36000), D(40000), G.TOTALS),
            c("EE_PF", D(2160), D(2400)),
            c("NET_PAY", D(33640), D(37400), G.TOTALS),
        ]
        assert explain_net(results) == "Net difference is fully explained by: gross +4000, EE_PF -240"

    def test_explain_net_does_not_reconcile(self):
        results = [c("NET_PAY", D(79800), D(79700), G.TOTALS)]
        assert explain_net(results) is None
        assert explain_net([c("NET_PAY", D(1), D(1), G.TOTALS)]) is None
