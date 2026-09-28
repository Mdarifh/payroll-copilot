"""Small HTTP client for the Payroll Copilot API (used by the Streamlit UI)."""

from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class ApiClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        api_key: str | None = None,
        timeout: float = 120.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        headers = {"X-API-Key": api_key} if api_key else {}
        self.http = http_client or httpx.Client(base_url=base_url, timeout=timeout)
        self.http.headers.update(headers)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"API not reachable ({exc.__class__.__name__})") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ApiError(response.status_code, str(detail))
        return response.json()

    # ------------------------------------------------------------------ endpoints

    def health(self) -> dict:
        return self._request("GET", "/health")

    def employees(self) -> list[dict]:
        return self._request("GET", "/employees")

    def employee_view(self, kind: str, code: str, period: str) -> dict:
        """kind: attendance | salary-structure | payroll | payslip"""
        return self._request("GET", f"/employees/{code}/{kind}", params={"period": period})

    def calculate(self, kind: str, code: str, period: str | None = None, **extra: Any) -> dict:
        body: dict[str, Any] = {"employee_code": code, **extra}
        if period is not None:
            body["period"] = period
        return self._request("POST", f"/calculate/{kind}", json=body)

    def validate_employee(self, code: str, period: str) -> dict:
        return self._request("POST", "/validate/employee",
                             json={"employee_code": code, "period": period})  # fmt: skip

    def validate_payroll(self, period: str) -> dict:
        return self._request("POST", "/validate/payroll", json={"period": period})

    def qa_runs(self) -> list[dict]:
        return self._request("GET", "/qa/runs")

    def kb_search(self, query: str, **filters: Any) -> dict:
        body = {"query": query, **{k: v for k, v in filters.items() if v not in (None, "")}}
        return self._request("POST", "/kb/search", json=body)

    def chat(self, message: str, thread_id: str, user: str | None = None) -> dict:
        headers = {"X-User": user} if user else {}
        return self._request("POST", "/chat", json={"message": message, "thread_id": thread_id},
                             headers=headers)  # fmt: skip

    # approvals (X-User identifies the person acting)

    def approvals(self, status: str | None = None) -> list[dict]:
        return self._request("GET", "/approvals", params={"status": status} if status else None)

    def request_approval(self, action_type: str, payload: dict, user: str,
                         reason: str | None = None) -> dict:  # fmt: skip
        return self._request("POST", "/approvals", headers={"X-User": user},
                             json={"action_type": action_type, "payload": payload,
                                   "reason": reason})  # fmt: skip

    def decide(self, approval_id: int, approve: bool, user: str,
               comment: str | None = None) -> dict:  # fmt: skip
        verb = "approve" if approve else "reject"
        return self._request("POST", f"/approvals/{approval_id}/{verb}",
                             headers={"X-User": user}, json={"comment": comment})  # fmt: skip
