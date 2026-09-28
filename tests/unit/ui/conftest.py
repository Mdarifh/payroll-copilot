from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from payroll_copilot.agent.copilot import PayrollCopilot
from payroll_copilot.api.main import create_app
from payroll_copilot.ui.api_client import ApiClient


@pytest.fixture
def api_app(seeded_session, kb, make_settings):
    @contextmanager
    def sessions():
        yield seeded_session

    def _make(**settings):
        return create_app(
            make_settings(**settings), sessions=sessions, knowledge_base=lambda: kb,
            copilot_factory=lambda ctx: PayrollCopilot(ctx, use_configured_llm=False),
        )  # fmt: skip

    return _make


@pytest.fixture
def api_client(api_app) -> ApiClient:
    """UI client wired to the real FastAPI app in-process (no network)."""
    return ApiClient(http_client=TestClient(api_app()))
