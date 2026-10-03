"""Salary heads and structure templates from config/payroll/salary_structures.yaml."""

from functools import lru_cache
from pathlib import Path

from payroll_copilot.core.config import get_settings
from payroll_copilot.domain.salary import SalaryHeadInfo, StructureTemplate
from payroll_copilot.rules.loader import read_yaml


@lru_cache
def _load(path: Path) -> tuple[dict[str, SalaryHeadInfo], dict[str, StructureTemplate]]:
    data = read_yaml(path)
    heads = {h["code"]: SalaryHeadInfo(**h) for h in data["salary_heads"]}
    templates = {
        s["code"]: StructureTemplate(code=s["code"], version=s["version"], name=s["name"],
                                     components=s["components"])
        for s in data["salary_structures"]
    }  # fmt: skip
    return heads, templates


def salary_heads(path: Path | None = None) -> dict[str, SalaryHeadInfo]:
    return _load(path or get_settings().salary_heads_file)[0]


def structure_templates(path: Path | None = None) -> dict[str, StructureTemplate]:
    return _load(path or get_settings().salary_heads_file)[1]
