from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from payroll_copilot.agent.copilot import PayrollCopilot
from payroll_copilot.api.main import create_app
from payroll_copilot.services.actual_payroll_seed import seed_actual_payroll

KEY = {"X-API-Key": "test-key-1234"}


@pytest.fixture
def make_client(seeded_session, kb, make_settings):
    @contextmanager
    def sessions():
        yield seeded_session

    def _make(**settings) -> TestClient:
        app = create_app(
            make_settings(**settings), sessions=sessions, knowledge_base=lambda: kb,
            copilot_factory=lambda ctx: PayrollCopilot(ctx, use_configured_llm=False),
        )  # fmt: skip
        return TestClient(app)

    return _make


@pytest.fixture
def client(make_client):
    return make_client()


class TestHealthAndAuth:
    def test_health_is_open(self, make_client):
        r = make_client(api_key="test-key-1234").get("/health")
        assert r.status_code == 200 and r.json()["database"] == "ok"

    def test_api_key_required_when_configured(self, make_client):
        c = make_client(api_key="test-key-1234")
        assert c.get("/employees").status_code == 401
        assert c.get("/employees", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/employees", headers=KEY).status_code == 200

    def test_open_in_dev_without_key(self, client):
        assert client.get("/employees").status_code == 200

    def test_openapi_documents_endpoints(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        for path in ("/employees/{employee_code}/payroll", "/calculate/pf", "/validate/payroll",
                     "/chat", "/kb/search", "/qa/runs"):  # fmt: skip
            assert path in paths


class TestEmployees:
    def test_list_and_get(self, client):
        employees = client.get("/employees").json()
        assert len(employees) == 10 and employees[0]["employee_code"] == "ARIF-112"
        r = client.get("/employees/arif-112").json()
        assert r["data"]["employee"]["uan"] == "********0112"

    def test_not_found_and_invalid(self, client):
        r = client.get("/employees/EMP-999")
        assert r.status_code == 404 and r.json()["error"] == "NotFoundError"
        r = client.get("/employees/EMP 2")
        assert r.status_code == 422 and "not a valid employee code" in r.json()["detail"]

    def test_attendance_structure_payslip(self, client):
        r = client.get("/employees/EMP-002/attendance", params={"period": "2026-09"})
        assert r.status_code == 200 and "Paid days 27 of 30" in r.json()["text"]
        r = client.get("/employees/EMP-007/salary-structure", params={"period": "2026-09"})
        assert "[NO ] Employee PF" in r.json()["text"]
        r = client.get("/employees/EMP-002/payslip", params={"period": "2026-09"})
        assert r.json()["data"]["payslip"]["net_pay"] == "33640"

    def test_bad_period(self, client):
        r = client.get("/employees/EMP-002/attendance", params={"period": "Sep-2026"})
        assert r.status_code == 422 and "YYYY-MM" in r.json()["detail"]

    def test_missing_attendance_is_404(self, client):
        r = client.get("/employees/EMP-005/attendance", params={"period": "2026-08"})
        assert r.status_code == 404 and "No attendance" in r.json()["detail"]

    def test_actual_payroll(self, client, seeded_session):
        r = client.get("/employees/ARIF-112/payroll", params={"period": "2026-09"})
        assert r.status_code == 404
        seed_actual_payroll(seeded_session)
        r = client.get("/employees/ARIF-112/payroll", params={"period": "2026-09"})
        assert r.status_code == 200 and r.json()["data"]["actual"]["deductions"]["EE_PF"] == "1800"


class TestCalculations:
    def test_pf_money_is_string_with_traces(self, client):
        r = client.post("/calculate/pf", json={"employee_code": "ARIF-112", "period": "2026-09"})
        data = r.json()["data"]
        assert data["pf"]["amounts"]["EE_PF"] == "3000"
        assert isinstance(data["pf"]["pf_wage"], str)
        assert {t["component"] for t in data["traces"]} >= {"EE_PF", "EPS"}
        assert data["traces"][0]["rule"]["rule_code"] == "EPF"

    @pytest.mark.parametrize(("path", "code", "needle"), [
        ("/calculate/salary", "EMP-005", "GROSS EARNINGS"),
        ("/calculate/esic", "EMP-003", "COVERED"),
        ("/calculate/pt", "EMP-004", "₹1,250"),
        ("/calculate/arrear", "EMP-004", "ARREARS"),
        ("/calculate/payroll", "EMP-009", "NET PAY"),
        ("/calculate/pf-rules", "ARIF-112", "EPF v1"),
        ("/calculate/tds", "EMP-009", "Monthly TDS"),
    ])  # fmt: skip
    def test_endpoints(self, client, path, code, needle):
        r = client.post(path, json={"employee_code": code, "period": "2026-09"})
        assert r.status_code == 200 and needle in r.json()["text"]

    def test_tds_override_and_validation(self, client):
        body = {"employee_code": "EMP-009", "period": "2026-09", "override_amount": "3000"}
        assert client.post("/calculate/tds", json=body).json()["data"]["tds"]["monthly_tds"] == "3000"
        body["override_amount"] = "-1"
        assert client.post("/calculate/tds", json=body).status_code == 422

    def test_fnf(self, client):
        r = client.post("/calculate/fnf", json={"employee_code": "EMP-006"})
        assert r.json()["data"]["net_settlement"] == "34000"
        r = client.post("/calculate/fnf", json={"employee_code": "ARIF-112"})
        assert r.status_code == 404

    def test_body_validation(self, client):
        r = client.post("/calculate/pf", json={"employee_code": "ARIF-112", "period": "2026/13"})
        assert r.status_code == 422


class TestValidationAndQA:
    def test_validate(self, client, seeded_session):
        seed_actual_payroll(seeded_session)
        r = client.post("/validate/employee", json={"employee_code": "EMP-008", "period": "2026-09"})
        assert "[SLAB]" in r.json()["text"]
        r = client.post("/validate/payroll", json={"period": "2026-09"})
        data = r.json()["data"]
        assert data["summary"]["findings"] == 8 and len(data["findings"]) == 8

    def test_qa_runs_read_only(self, client, seeded_session):
        from payroll_copilot.core.dates import PayrollPeriod
        from payroll_copilot.services.qa_service import QAService

        assert client.get("/qa/runs").json() == []
        assert client.get("/qa/runs/1").status_code == 404
        seed_actual_payroll(seeded_session)
        service = QAService(seeded_session)
        report, _, validation = service.run(PayrollPeriod(2026, 9))
        run_id = service.save(report, validation)
        runs = client.get("/qa/runs").json()
        assert runs[0]["id"] == run_id and runs[0]["findings"] == 8
        detail = client.get(f"/qa/runs/{run_id}").json()
        assert len(detail["issues"]) == 64 and any(i["primary"] for i in detail["issues"])
        assert client.post("/qa/runs", json={"period": "2026-09"}).status_code == 405


class TestKnowledgeAndChat:
    def test_kb_search(self, client):
        r = client.post("/kb/search", json={"query": "PF wage ceiling", "module": "pf",
                                            "period": "2026-08", "k": 2})  # fmt: skip
        results = r.json()["results"]
        assert results and all(x["citation"].startswith("EPF v1") for x in results)

    def test_chat_with_memory(self, client):
        first = client.post("/chat", json={"message": "ARIF-112 ka PF 2026-09", "thread_id": "u1"})
        assert first.json()["tools_used"] == ["calculate_pf"]
        follow = client.post("/chat", json={"message": "aur ESIC?", "thread_id": "u1"}).json()
        assert follow["intent"] == "ESIC" and follow["entities"]["employee_source"] == "memory"

    def test_chat_validation(self, client):
        assert client.post("/chat", json={"message": ""}).status_code == 422
        assert client.post("/chat", json={"message": "hi", "thread_id": "../x"}).status_code == 422

    def test_tools_listing(self, client):
        tools = client.get("/tools").json()
        assert {t["access"] for t in tools} == {"read"} and len(tools) == 17
