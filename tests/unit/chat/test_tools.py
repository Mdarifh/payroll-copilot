"""Tools return engine/database facts for the logged-in user only, with role checks."""

from decimal import Decimal

import pytest

from payroll_copilot.chat.tools import TOOLS, ToolError, run_tool, tools_for
from payroll_copilot.domain.enums import Role


def test_tool_sets_per_role():
    employee = {t.name for t in tools_for(Role.EMPLOYEE)}
    hr = {t.name for t in tools_for(Role.HR)}
    assert employee == {"get_payslip", "get_leave_balance", "get_tax_summary", "explain_my_pay",
                        "compare_my_months", "payroll_rules"}
    assert hr == employee | {"get_attendance_exceptions", "start_payroll_review",
                             "find_employee", "explain_employee_pay", "compare_employee_months"}


def test_employee_tools_have_no_employee_parameter():
    for tool in TOOLS:
        if Role.EMPLOYEE in tool.roles:
            params = set(tool.parameters["properties"])
            assert not params & {"emp_id", "employee_id", "employee_code", "employee"}, tool.name
        assert tool.parameters["additionalProperties"] is False


def test_payslip_latest_processed(make_ctx):
    result = run_tool(make_ctx("EMP-101"), "get_payslip", {})
    assert result.content["period"] == "2026-08" and result.content["net_pay"] == "58000"
    assert result.content["deductions"] == {"Employee PF": "1800", "Professional Tax": "200",
                                            "Income Tax (TDS)": "0"}  # fmt: skip
    card = result.cards[0]
    assert card["type"] == "payslip_card" and card["payslip"]["employee_code"] == "EMP-101"
    assert card["download_url"] == "/payslips/2026-08/pdf"


@pytest.mark.parametrize("month", ["2026-08", "Aug 2026", "august, 2026", "08/2026"])
def test_payslip_month_formats(make_ctx, month):
    assert run_tool(make_ctx(), "get_payslip", {"month": month}).content["period"] == "2026-08"


def test_payslip_not_processed_and_bad_month(make_ctx):
    with pytest.raises(ToolError, match="Sep-2026 is not processed yet"):
        run_tool(make_ctx(), "get_payslip", {"month": "2026-09"})
    with pytest.raises(ToolError, match="Invalid arguments.*YYYY-MM"):
        run_tool(make_ctx(), "get_payslip", {"month": "next friday"})
    with pytest.raises(ToolError, match="No payslip has been processed"):
        run_tool(make_ctx("EMP-105"), "get_payslip", {})  # joined in September


def test_employee_cannot_pass_another_employee(make_ctx):
    with pytest.raises(ToolError, match="Invalid arguments.*emp_id"):
        run_tool(make_ctx("EMP-101"), "get_payslip", {"emp_id": "EMP-102"})


def test_leave_balance(make_ctx):
    content = run_tool(make_ctx("EMP-103"), "get_leave_balance", {}).content
    assert content["financial_year"] == "2026-27"
    by_type = {b["leave_type"]: b for b in content["balances"]}
    assert by_type["SL"] == {"leave_type": "SL", "entitlement": "12", "taken": "1",
                             "pending": "0", "available": "11"}  # fmt: skip
    assert by_type["EL"]["pending"] == "2" and by_type["EL"]["available"] == "16"
    assert content["pending_requests"] == [{"leave_type": "EL", "from": "2026-10-08",
                                            "to": "2026-10-09", "days": "2"}]  # fmt: skip


def test_tax_summary(make_ctx):
    content = run_tool(make_ctx("EMP-104"), "get_tax_summary", {}).content
    assert (content["financial_year"], content["regime"]) == ("2026-27", "OLD")
    assert content["based_on_month"] == "2026-09"
    assert Decimal(content["total_annual_tax"]) > 0
    # August payslip: deductions 3750 = PF 1800 + TDS 1950 (TN PT is not due in August)
    assert content["tds_deducted_in_processed_payslips"] == "1950"
    with pytest.raises(ToolError, match="No attendance or salary data for FY 2027-28"):
        run_tool(make_ctx("EMP-104"), "get_tax_summary", {"financial_year": "2027-28"})


def test_attendance_exceptions_hr_only(make_ctx):
    with pytest.raises(ToolError, match="not allowed for the employee role"):
        run_tool(make_ctx("EMP-101"), "get_attendance_exceptions",
                 {"emp_id": "EMP-102", "month": "2026-09"})  # fmt: skip
    content = run_tool(make_ctx("HR-001"), "get_attendance_exceptions",
                       {"emp_id": "emp-102", "month": "Sep 2026"}).content  # fmt: skip
    assert content["employee_name"] == "Priya Nair"
    assert [e["date"] for e in content["unapproved_absences"]] == ["2026-09-24", "2026-09-25"]
    assert content["total_unapproved_days"] == "2" and content["lop_decision"] is None
    assert content["approved_leaves"][0]["leave_type"] == "CL"
    assert content["payroll_processed"] is False
    with pytest.raises(ToolError, match="EMP-999 not found"):
        run_tool(make_ctx("HR-001"), "get_attendance_exceptions",
                 {"emp_id": "EMP-999", "month": "2026-09"})  # fmt: skip


def test_unknown_tool_and_audit_rows(make_ctx):
    ctx = make_ctx()
    run_tool(ctx, "get_leave_balance", {})
    with pytest.raises(ToolError, match="Unknown tool"):
        run_tool(ctx, "delete_everything", {})
    with pytest.raises(ToolError):
        run_tool(ctx, "get_attendance_exceptions", {"emp_id": "EMP-102", "month": "2026-09"})
    rows = [(r.entity_id, r.new_value["allowed"]) for r in ctx.audit_rows]
    assert rows == [("get_leave_balance", True), ("delete_everything", False),
                    ("get_attendance_exceptions", False)]  # fmt: skip
    assert all(r.actor_id == ctx.user.id and r.request_id == "req-test" for r in ctx.audit_rows)
