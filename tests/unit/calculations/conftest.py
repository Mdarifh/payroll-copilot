import pytest

from payroll_copilot.domain.salary import SalaryHeadInfo, StructureTemplate
from payroll_copilot.services.salary_config import salary_heads, structure_templates


@pytest.fixture(scope="session")
def heads() -> dict[str, SalaryHeadInfo]:
    return salary_heads()


@pytest.fixture(scope="session")
def templates() -> dict[str, StructureTemplate]:
    return structure_templates()
