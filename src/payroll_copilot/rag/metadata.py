"""Document metadata, read from a sidecar ``<document>.meta.yaml`` (never guessed).

Chroma metadata values must be scalars, so dates are also stored as integers
(YYYYMMDD) and an open-ended ``effective_to`` becomes 99991231; that makes
"effective in period" a simple numeric range filter.
"""

from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.domain.enums import RuleStatus

KB_MODULES = ("pf", "esic", "pt", "lwf", "tds", "payroll", "attendance", "arrears", "fnf",
              "compliance")  # fmt: skip
OPEN_END = 99991231


class KnowledgeBaseError(PayrollCopilotError):
    """Invalid knowledge base document or metadata."""


def date_int(value: date | None, default: int = OPEN_END) -> int:
    return default if value is None else value.year * 10000 + value.month * 100 + value.day


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    module: str
    document_name: str
    rule_name: str | None = None
    version: str = "1"
    effective_from: date
    effective_to: date | None = None
    jurisdiction: str = Field(pattern=r"^IN(-[A-Z]{2,4})?$")  # IN or IN-<STATE>
    source: str
    status: RuleStatus = RuleStatus.UNVERIFIED
    doc_type: str = "rule"  # rule | policy | guide

    @model_validator(mode="after")
    def _valid(self) -> "DocumentMetadata":
        if self.module not in KB_MODULES:
            raise ValueError(f"module must be one of {KB_MODULES}")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to before effective_from")
        return self

    def to_chroma(self) -> dict[str, Any]:
        """Flat, scalar-only metadata for the vector store."""
        return {
            "module": self.module,
            "document_name": self.document_name,
            "rule_name": self.rule_name or "",
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else "open",
            "effective_from_int": date_int(self.effective_from),
            "effective_to_int": date_int(self.effective_to),
            "jurisdiction": self.jurisdiction,
            "source": self.source,
            "status": self.status.value,
            "doc_type": self.doc_type,
        }


def sidecar_path(document: Path) -> Path:
    return document.with_name(document.name + ".meta.yaml")


def load_metadata(document: Path, kb_root: Path) -> DocumentMetadata:
    from payroll_copilot.rules.loader import read_yaml

    path = sidecar_path(document)
    if not path.exists():
        raise KnowledgeBaseError(f"{document}: metadata sidecar {path.name} is missing")
    try:
        meta = DocumentMetadata.model_validate(read_yaml(path))
    except ValidationError as exc:
        raise KnowledgeBaseError(f"{path}: invalid metadata\n{exc}") from exc
    folder = document.relative_to(kb_root).parts[0]
    if folder != meta.module:
        raise KnowledgeBaseError(
            f"{document}: stored under '{folder}/' but metadata module is '{meta.module}'"
        )
    return meta
