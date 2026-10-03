import httpx
import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.services.actual_payroll_seed import seed_actual_payroll
from payroll_copilot.ui.api_client import ApiClient, ApiError

APP = PROJECT_ROOT / "src" / "payroll_copilot" / "ui" / "streamlit_app.py"


class TestApiClient:
    def test_calls(self, api_client):
        assert api_client.health()["database"] == "ok"
        assert len(api_client.employees()) == 10
        assert api_client.health()["latest_period"] == "2026-09"  # newest attendance month
        pf = api_client.calculate("pf", "ARIF-112", "2026-09")
        assert pf["data"]["pf"]["amounts"]["EE_PF"] == "3000"
        view = api_client.employee_view("attendance", "EMP-002", "2026-09")
        assert "Paid days 27 of 30" in view["text"]

    def test_errors(self, api_client):
        with pytest.raises(ApiError) as missing:
            api_client.calculate("pf", "EMP-999", "2026-09")
        assert missing.value.status == 404 and "not found" in missing.value.detail
        with pytest.raises(ApiError) as invalid:
            api_client.calculate("pf", "ARIF-112", "2026-13")
        assert invalid.value.status == 422

    def test_api_key_is_sent(self, api_app):
        app = api_app(api_key="ui-key-5678")
        with pytest.raises(ApiError) as denied:
            ApiClient(http_client=TestClient(app)).employees()
        assert denied.value.status == 401
        assert len(ApiClient(api_key="ui-key-5678", http_client=TestClient(app)).employees()) == 10

    def test_unreachable(self):
        def refuse(request):
            raise httpx.ConnectError("refused")

        client = ApiClient(http_client=httpx.Client(transport=httpx.MockTransport(refuse),
                                                    base_url="http://x"))  # fmt: skip
        with pytest.raises(ApiError) as down:
            client.health()
        assert down.value.status == 0 and "not reachable" in down.value.detail


@pytest.fixture
def app(api_client):
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state["client"] = api_client
    return at.run()


class TestStreamlitApp:
    def test_renders(self, app):
        assert not app.exception
        assert app.title[0].value == "🧾 AI Payroll Copilot"
        assert "API connected" in app.sidebar.success[0].value
        employee = app.sidebar.selectbox(key="employee")
        assert employee.value == "ARIF-112" and len(employee.options) == 10
        assert app.sidebar.selectbox(key="period").value == "2026-09"

    def test_chat_with_sidebar_context(self, app):
        app.chat_input(key="chat_input").set_value("PF kitna hai?").run()
        assert not app.exception
        user, answer = app.session_state["messages"]
        assert user["content"] == "PF kitna hai? (employee ARIF-112, period 2026-09)"
        assert "Employee PF        ₹3,000" in answer["content"]
        assert answer["reply"]["tools_used"] == ["calculate_pf"]

    def test_calculation_tab(self, app):
        app.selectbox(key="calc_choice").set_value("Expected payslip")
        app.button(key="calc_run").click().run()
        assert not app.exception
        assert any("NET PAY" in c.value for c in app.code)

    def test_expected_vs_actual_without_actual_run(self, app):
        app.button(key="eva_run").click().run()
        assert not app.exception
        assert any("No actual payslip" in i.value for i in app.info)

    def test_qa_tab(self, app, seeded_session):
        seed_actual_payroll(seeded_session)
        app.button(key="qa_run").click().run()
        assert not app.exception
        metrics = {m.label: m.value for m in app.metric}
        assert metrics["Findings"] == "8" and metrics["Clean"] == "3"
        app.toggle(key="eva_only_issues").set_value(True).run()  # any rerun keeps the report
        assert {m.label: m.value for m in app.metric}["Findings"] == "8"

    def test_qa_save_request_from_ui(self, app, seeded_session):
        seed_actual_payroll(seeded_session)
        app.button(key="qa_run").click().run()
        app.button(key="qa_save").click().run()
        assert not app.exception
        assert any("Approval request #" in s.value for s in app.success)
        pending = app.session_state["client"].approvals("PENDING")
        assert pending[0]["action_type"] == "SAVE_QA_RUN"
        assert pending[0]["requested_by"] == "api:hr.user"

    def test_policy_tab(self, app):
        app.button(key="kb_run").click().run()
        assert not app.exception
        assert any("EPF v2" in e.label for e in app.expander)

    def test_approvals_tab(self, api_app):
        client = ApiClient(http_client=TestClient(api_app(approvers=["priya.hr"])))
        request = client.request_approval("TDS_OVERRIDE", {
            "employee_code": "EMP-009", "period": "2026-09", "amount": "3000",
            "reason": "employee asked"}, user="hr.user")  # fmt: skip
        at = AppTest.from_file(str(APP), default_timeout=300)
        at.session_state["client"] = client
        at.run()
        assert any("Override monthly TDS of EMP-009" in m.value for m in at.markdown)
        at.button(key=f"approve_{request['id']}").click().run()  # hr.user is not an approver
        assert any("not an approver" in e.value for e in at.error)
        at.sidebar.text_input(key="acting_as").set_value("priya.hr").run()
        at.button(key=f"approve_{request['id']}").click().run()
        assert not at.exception
        assert any("EXECUTED" in s.value for s in at.success)
        assert client.approvals("EXECUTED")[0]["decided_by"] == "priya.hr"

    def test_api_down_message(self):
        def refuse(request):
            raise httpx.ConnectError("refused")

        at = AppTest.from_file(str(APP), default_timeout=120)
        at.session_state["client"] = ApiClient(
            http_client=httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://x"))
        at.run()
        assert any("Start the API first" in e.value for e in at.sidebar.error)
