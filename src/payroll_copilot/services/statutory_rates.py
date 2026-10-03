"""Statutory rates: YAML -> statutory_rates table -> rate sets for any period."""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.pf_rates import RateRow, validate_rows
from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.core.exceptions import RuleConfigError, RuleImmutableError
from payroll_copilot.db.models import StatutoryRate
from payroll_copilot.domain.enums import RuleStatus
from payroll_copilot.rules.loader import read_yaml

PF_RATES_FILE = PROJECT_ROOT / "config" / "statutory" / "pf_rates.yaml"
_FIELDS = ("rule_version", "rate_value", "wage_ceiling", "effective_to", "source", "status")


@dataclass
class RateSyncReport:
    inserted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0

    def __str__(self) -> str:
        return (f"Statutory rates: inserted {len(self.inserted)}, updated {len(self.updated)}, "
                f"unchanged {self.unchanged}")  # fmt: skip


def _row(r: dict) -> RateRow:
    ceiling = r.get("wage_ceiling")
    return RateRow(r["rate_name"], r["rule_version"], Decimal(str(r["rate_value"])),
                   None if ceiling is None else Decimal(str(ceiling)), r["effective_from"],
                   r.get("effective_to"), r["source"], str(r["status"]))  # fmt: skip


def load_rate_file(path: Path = PF_RATES_FILE) -> tuple[str, list[dict]]:
    data = read_yaml(path)
    sources = data.get("sources", {})
    rows = []
    for r in data["rates"]:
        if r["source"] not in sources:
            raise RuleConfigError(f"{path}: unknown source {r['source']!r} for {r['rate_name']}")
        rows.append({**r, "source": sources[r["source"]]})
    problems = validate_rows([_row(r) for r in rows])
    if problems:
        raise RuleConfigError(f"{path}: " + "; ".join(problems))
    return data["rule_family"], rows


def _hash(r: dict) -> str:
    payload = {k: str(r.get(k)) for k in ("rate_name", "effective_from", *_FIELDS)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def sync_rates(session: Session, path: Path = PF_RATES_FILE) -> RateSyncReport:
    """Upsert rows by (family, rate_name, effective_from). VERIFIED rows never change."""
    family, rows = load_rate_file(path)
    report = RateSyncReport()
    for r in rows:
        key = f"{r['rate_name']}@{r['effective_from']}"
        existing = session.scalar(select(StatutoryRate).where(
            StatutoryRate.rule_family == family, StatutoryRate.rate_name == r["rate_name"],
            StatutoryRate.effective_from == r["effective_from"]))  # fmt: skip
        digest = _hash(r)
        row = _row(r)
        values = {"rule_version": row.rule_version, "rate_value": row.rate_value,
                  "wage_ceiling": row.wage_ceiling, "effective_to": row.effective_to,
                  "source": row.source, "status": RuleStatus(row.status),
                  "content_hash": digest}  # fmt: skip
        if existing is None:
            session.add(StatutoryRate(rule_family=family, rate_name=row.rate_name,
                                      effective_from=row.effective_from, **values))
            report.inserted.append(key)
        elif existing.content_hash == digest:
            report.unchanged += 1
        elif existing.status is RuleStatus.VERIFIED:
            raise RuleImmutableError(f"{key} is VERIFIED and cannot change - add a new "
                                     "effective-dated row instead")  # fmt: skip
        else:
            for name, value in values.items():
                setattr(existing, name, value)
            report.updated.append(key)
    session.flush()
    return report


class StatutoryRateService:
    def __init__(self, session: Session, family: str = "PF") -> None:
        self.session = session
        self.family = family

    def rows(self) -> list[RateRow]:
        rows = self.session.scalars(select(StatutoryRate).where(
            StatutoryRate.rule_family == self.family).order_by(StatutoryRate.effective_from))
        return [RateRow(r.rate_name, r.rule_version, r.rate_value, r.wage_ceiling,
                        r.effective_from, r.effective_to, r.source, r.status.value)
                for r in rows]  # fmt: skip
