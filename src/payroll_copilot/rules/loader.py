"""Load rule YAML files, validate them and sync them into the database.

Sync behaviour per (rule_code, version):
* new            -> inserted
* same content   -> unchanged (status changes are still applied)
* changed        -> updated, unless the stored row is VERIFIED; verified values
                    are immutable and a new version must be added instead.
"""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.exceptions import RuleConfigError, RuleImmutableError
from payroll_copilot.db.models import ESICRule, LWFRule, PFRule, PTRule, PTSlab, TDSRule, TDSSlab
from payroll_copilot.domain.enums import RuleStatus
from payroll_copilot.rules.schemas import (
    ESICRuleFile,
    LWFRuleFile,
    PFRuleFile,
    PTRuleFile,
    PTVersion,
    RuleFile,
    RuleFileBase,
    TDSRuleFile,
    TDSVersion,
    VersionBase,
)

_RULE_FILE_ADAPTER: TypeAdapter = TypeAdapter(RuleFile)

_MODEL_BY_FILE = {
    PFRuleFile: PFRule,
    ESICRuleFile: ESICRule,
    PTRuleFile: PTRule,
    LWFRuleFile: LWFRule,
    TDSRuleFile: TDSRule,
}

# Fields that live in child tables or are handled separately, not as columns.
_NON_COLUMN_FIELDS = {"slab_sets", "slabs", "status", "applicability", "deduction_months"}


class _DecimalSafeLoader(yaml.SafeLoader):
    """YAML loader that parses floats as Decimal, so 8.33 stays exactly 8.33."""


def _construct_decimal(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Decimal:
    return Decimal(loader.construct_scalar(node).replace("_", ""))


_DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)


def read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_DecimalSafeLoader)  # noqa: S506 - SafeLoader subclass


def parse_rule_file(data: Any, source: str = "<data>") -> RuleFileBase:
    try:
        return _RULE_FILE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise RuleConfigError(f"{source}: invalid rule file\n{exc}") from exc


def load_rule_files(rules_dir: Path) -> list[RuleFileBase]:
    """Parse every ``*.yaml`` under ``rules_dir``; rule codes must be unique."""
    if not rules_dir.is_dir():
        raise RuleConfigError(f"Rules directory not found: {rules_dir}")
    files = []
    seen: dict[str, Path] = {}
    for path in sorted(rules_dir.rglob("*.yaml")):
        rule_file = parse_rule_file(read_yaml(path), source=str(path))
        if rule_file.rule_code in seen:
            raise RuleConfigError(
                f"rule_code {rule_file.rule_code} defined in both {seen[rule_file.rule_code]} "
                f"and {path}"
            )
        seen[rule_file.rule_code] = path
        files.append(rule_file)
    return files


@dataclass
class LoadReport:
    inserted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.inserted) + len(self.updated) + len(self.unchanged)


def _content_hash(family: dict[str, Any], version: VersionBase) -> str:
    payload = {"family": family, "version": version.model_dump(mode="json", exclude={"status"})}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _column_values(rule_file: RuleFileBase, version: VersionBase) -> dict[str, Any]:
    values = version.model_dump(exclude=_NON_COLUMN_FIELDS)
    values.update(rule_file.family_fields())
    values["rule_code"] = rule_file.rule_code
    values["applicability"] = [c.model_dump(mode="json") for c in version.applicability]
    if isinstance(version, PTVersion):
        values["deduction_months"] = version.effective_deduction_months
    elif hasattr(version, "deduction_months"):
        values["deduction_months"] = list(version.deduction_months)
    if "surcharge" in values:
        values["surcharge"] = [s.model_dump(mode="json") for s in version.surcharge]
    if "deduction_limits" in values:
        values["deduction_limits"] = {k: str(v) for k, v in version.deduction_limits.items()}
    return values


def _child_rows(version: VersionBase) -> list:
    if isinstance(version, PTVersion):
        rows, sequence = [], 0
        for slab_set in version.slab_sets:
            lower = None
            for slab in slab_set.slabs:
                rows.append(
                    PTSlab(
                        sequence=sequence,
                        gender=slab_set.gender,
                        lower_exclusive=lower,
                        upper_inclusive=slab.upto,
                        amount=slab.amount,
                        month_overrides={str(m): str(a) for m, a in slab.month_overrides.items()},
                    )
                )
                lower, sequence = slab.upto, sequence + 1
        return rows
    if isinstance(version, TDSVersion):
        rows, lower = [], None
        for sequence, slab in enumerate(version.slabs):
            rows.append(
                TDSSlab(
                    sequence=sequence,
                    lower_exclusive=lower,
                    upper_inclusive=slab.upto,
                    rate=slab.rate,
                )
            )
            lower = slab.upto
        return rows
    return []


def sync_rules(session: Session, rule_files: list[RuleFileBase]) -> LoadReport:
    report = LoadReport()
    for rule_file in rule_files:
        model = _MODEL_BY_FILE[type(rule_file)]
        family = rule_file.family_fields()
        for version in rule_file.versions:
            key = f"{rule_file.rule_code} v{version.version}"
            digest = _content_hash(family, version)
            row = session.scalar(
                select(model).where(
                    model.rule_code == rule_file.rule_code, model.version == version.version
                )
            )
            if row is None:
                row = model(**_column_values(rule_file, version), content_hash=digest)
                row.status = version.status
                if hasattr(model, "slabs"):
                    row.slabs = _child_rows(version)
                session.add(row)
                report.inserted.append(key)
                continue

            if row.content_hash == digest:
                # A version verified through an approval stays VERIFIED even if the YAML still
                # says UNVERIFIED; only an explicit RETIRED in the YAML changes it.
                if not (row.status is RuleStatus.VERIFIED
                        and version.status is RuleStatus.UNVERIFIED):  # fmt: skip
                    row.status = version.status
                report.unchanged.append(key)
                continue

            if row.status is RuleStatus.VERIFIED:
                raise RuleImmutableError(
                    f"{key} is VERIFIED; add a new version instead of editing its values"
                )
            for column, value in _column_values(rule_file, version).items():
                setattr(row, column, value)
            row.content_hash = digest
            row.status = version.status
            if hasattr(model, "slabs"):
                row.slabs = _child_rows(version)
            report.updated.append(key)
    session.flush()
    return report
