"""Mask personal identifiers and secrets before anything is written to the audit log."""

import re
from collections.abc import Iterable
from typing import Any

_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_LONG_DIGITS = re.compile(r"\b\d{12}\b")  # UAN / Aadhaar
_MAX_TEXT = 2000


def _mask(match: re.Match[str]) -> str:
    value = match.group(0)
    return "*" * (len(value) - 4) + value[-4:]


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively mask PAN, 12-digit identifiers and known secrets; truncate long text."""
    secrets = [s for s in secrets if s]
    if isinstance(value, str):
        text = _LONG_DIGITS.sub(_mask, _PAN.sub(_mask, value))
        for secret in secrets:
            text = text.replace(secret, "****")
        return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "…"
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, secrets) for v in value]
    return value
