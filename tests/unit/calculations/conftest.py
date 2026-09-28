import pytest

from payroll_copilot.db.seed import DEFAULT_SEED_FILE
from payroll_copilot.domain.salary import SalaryHeadInfo, StructureTemplate
from payroll_copilot.rules.loader import read_yaml


@pytest.fixture(scope="session")
def seed_data() -> dict:
    return read_yaml(DEFAULT_SEED_FILE)


@pytest.fixture(scope="session")
def heads(seed_data) -> dict[str, SalaryHeadInfo]:
    return {h["code"]: SalaryHeadInfo(**h) for h in seed_data["salary_heads"]}


@pytest.fixture(scope="session")
def templates(seed_data) -> dict[str, StructureTemplate]:
    return {
        s["code"]: StructureTemplate(code=s["code"], version=s["version"], name=s["name"],
                                     components=s["components"])
        for s in seed_data["salary_structures"]
    }  # fmt: skip
