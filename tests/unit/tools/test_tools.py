from contextlib import contextmanager

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import func, select

from payroll_copilot.db.models import Employee, PayrollRun
from payroll_copilot.services.actual_payroll_seed import seed_actual_payroll
from payroll_copilot.tools import TOOL_ACCESS, ToolContext, build_tools


@pytest.fixture
def tools(seeded_session, kb):
    @contextmanager
    def sessions():
        yield seeded_session

    return {t.name: t for t in build_tools(ToolContext(sessions, knowledge_base=lambda: kb))}


def call(tool, **args):
    """Invoke like an agent does (ToolCall) to get both content and artifact."""
    msg = tool.invoke({"name": tool.name, "args": args, "id": "call-1", "type": "tool_call"})
    assert isinstance(msg, ToolMessage)
    return msg.content, msg.artifact


class TestRegistry:
    def test_all_tools_registered_read_only(self, tools):
        assert set(tools) == set(TOOL_ACCESS)
        assert {t.metadata["access"] for t in tools.values()} == {"read"}
        assert all(t.response_format == "content_and_artifact" for t in tools.values())

    def test_llm_schemas(self, tools):
        spec = convert_to_openai_tool(tools["calculate_pf"])["function"]
        assert set(spec["parameters"]["required"]) == {"employee_code", "period"}
        assert "never recompute" in spec["description"]
        policy = convert_to_openai_tool(tools["search_payroll_policy"])["function"]
        assert policy["parameters"]["required"] == ["query"]
        assert "pf" in str(policy["parameters"]["properties"]["module"])


class TestEmployeeTools:
    def test_get_employee_masks_identifiers(self, tools):
        content, artifact = call(tools["get_employee"], employee_code="arif-112")
        assert artifact["employee"]["employee_code"] == "ARIF-112"
        assert artifact["employee"]["uan"] == "********0112"
        assert "100000000112" not in content

    def test_structure_and_attendance(self, tools):
        content, artifact = call(tools["get_salary_structure"], employee_code="EMP-007",
                                 period="2026-09")  # fmt: skip
        assert "[NO ] Employee PF" in content and artifact["ok"]
        content, artifact = call(tools["get_attendance"], employee_code="EMP-002", period="2026-09")
        assert "Paid days 27 of 30" in content
        assert artifact["traces"][0]["component"] == "PAID_DAYS"


class TestCalculationTools:
    def test_pf_with_trace(self, tools):
        content, artifact = call(tools["calculate_pf"], employee_code="ARIF-112", period="2026-09")
        assert "Employee PF        ₹3,000" in content and "min(30000, 25000)" in content
        assert artifact["pf"]["amounts"]["EE_PF"] == "3000"
        assert {t["component"] for t in artifact["traces"]} == {"EE_PF", "ER_EPF", "EPS", "EDLI",
                                                                "EPF_ADMIN"}  # fmt: skip

    def test_compare_pf_rules(self, tools):
        _, artifact = call(tools["compare_pf_rules"], employee_code="ARIF-112", period="2026-09")
        assert [r["amounts"]["EE_PF"] for r in artifact["rows"]] == ["1800", "3000"]

    @pytest.mark.parametrize(("name", "code", "needle"), [
        ("calculate_salary", "EMP-005", "GROSS EARNINGS"),
        ("calculate_esic", "EMP-003", "COVERED"),
        ("calculate_pt", "EMP-004", "half-yearly PT wage ₹3,10,000"),
        ("calculate_tds", "EMP-009", "Monthly TDS"),
        ("calculate_arrear", "EMP-004", "ARREARS"),
        ("get_payslip", "EMP-010", "NET PAY"),
    ])  # fmt: skip
    def test_calculations(self, tools, name, code, needle):
        content, artifact = call(tools[name], employee_code=code, period="2026-09")
        assert needle in content and artifact["ok"]

    def test_tds_what_if_override(self, tools):
        content, artifact = call(tools["calculate_tds"], employee_code="EMP-009", period="2026-09",
                                 override_amount="3000", override_reason="what-if")  # fmt: skip
        assert "(OVERRIDDEN)" in content and artifact["tds"]["monthly_tds"] == "3000"

    def test_no_arrear(self, tools):
        content, artifact = call(tools["calculate_arrear"], employee_code="ARIF-112", period="2026-09")
        assert "No salary arrears" in content and artifact["arrears"] is None

    def test_fnf(self, tools):
        _, artifact = call(tools["calculate_fnf"], employee_code="EMP-006")
        assert artifact["net_settlement"] == "34000"


class TestValidationTools:
    def test_before_actual_payroll_exists(self, tools):
        content, _ = call(tools["get_payroll"], employee_code="ARIF-112", period="2026-09")
        assert content == "No actual payslip for ARIF-112 in 2026-09"

    def test_with_actual_payroll(self, tools, seeded_session):
        seed_actual_payroll(seeded_session)
        content, artifact = call(tools["get_payroll"], employee_code="ARIF-112", period="2026-09")
        assert "deduction EE_PF: ₹1,800" in content
        content, artifact = call(tools["compare_expected_actual"], employee_code="ARIF-112",
                                 period="2026-09")  # fmt: skip
        assert "[RULE_VERSION]" in content
        issues = [r for r in artifact["validation"]["results"] if r["status"] == "MISMATCH"]
        assert {"EE_PF", "NET_PAY"} <= {r["component"] for r in issues}
        content, artifact = call(tools["validate_payroll"], period="2026-09")
        assert "with issues: 7" in content and len(artifact["employees"]) == 10

    def test_clean_employee(self, tools, seeded_session):
        seed_actual_payroll(seeded_session)
        content, _ = call(tools["compare_expected_actual"], employee_code="EMP-003", period="2026-09")
        assert content == "EMP-003 2026-09: all components match expected values"


class TestPolicyTool:
    def test_period_aware_search_with_sources(self, tools):
        content, artifact = call(tools["search_payroll_policy"], query="PF wage ceiling",
                                 module="pf", period="2026-08", k=2)  # fmt: skip
        assert artifact["found"] and "EPF v1" in content and "EPF v2" not in content
        assert artifact["sources"][0].startswith("[1] EPF v1")

    def test_not_found(self, tools):
        content, artifact = call(tools["search_payroll_policy"], query="income tax slabs",
                                 module="tds", period="2027-05")  # fmt: skip
        assert not artifact["found"] and "could not find" in content


class TestErrors:
    def test_unknown_employee_is_reported_not_raised(self, tools):
        content, artifact = call(tools["calculate_pf"], employee_code="EMP-999", period="2026-09")
        assert content == "ERROR: Employee EMP-999 not found"
        assert artifact == {"ok": False, "error": "Employee EMP-999 not found"}

    @pytest.mark.parametrize(("name", "args", "needle"), [
        ("calculate_pf", {"employee_code": "EMP 2", "period": "2026-09"}, "employee_code: 'EMP 2' is not a valid"),
        ("calculate_pf", {"employee_code": "EMP-002", "period": "Sep-2026"}, "period: Payroll period must be 'YYYY-MM'"),
        ("search_payroll_policy", {"query": "pf", "module": "salary"}, "module"),
        ("calculate_tds", {"employee_code": "EMP-002", "period": "2026-09", "override_amount": -5}, "override_amount"),
    ])  # fmt: skip
    def test_invalid_arguments(self, tools, name, args, needle):
        content = tools[name].invoke(args)
        assert content.startswith("ERROR: invalid arguments") and needle in content

    def test_missing_data_message(self, tools):
        content, _ = call(tools["get_attendance"], employee_code="EMP-005", period="2026-08")
        assert content == "ERROR: No attendance for EMP-005 in 2026-08"


def test_tools_do_not_write(tools, seeded_session):
    before = seeded_session.scalar(select(func.count()).select_from(Employee))
    for name in ("calculate_pf", "get_payslip", "compare_expected_actual"):
        call(tools[name], employee_code="EMP-002", period="2026-09")
    assert seeded_session.scalar(select(func.count()).select_from(PayrollRun)) == 0
    assert seeded_session.scalar(select(func.count()).select_from(Employee)) == before
    assert not seeded_session.new and not seeded_session.dirty


def test_read_scope_never_commits(temp_db_url):
    from payroll_copilot.__main__ import main
    from payroll_copilot.db.session import read_scope, session_scope

    main(["db", "seed"])
    with read_scope() as s:
        s.get(Employee, 1).full_name = "Changed"
    with session_scope() as s:
        assert s.get(Employee, 1).full_name != "Changed"
