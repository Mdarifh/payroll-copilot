"""Applicability conditions attached to rule versions.

A rule's ``applicability`` is a list of conditions that must ALL be true for the
employee context, e.g.::

    [{"field": "pf_applicable", "op": "eq", "value": true},
     {"field": "pf_join_date", "op": "gte", "value": "2026-09-01"}]

An empty list means "applies to everyone". A missing context field is an error,
not a silent non-match, so data gaps surface instead of picking a wrong rule.
"""

import operator
from collections.abc import Callable, Mapping
from datetime import date
from enum import StrEnum
from typing import Any

from payroll_copilot.core.exceptions import ApplicabilityError

_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "in": lambda actual, expected: actual in expected,
    "not_in": lambda actual, expected: actual not in expected,
}

SUPPORTED_OPERATORS = frozenset(_OPERATORS)


def _normalise(value: Any) -> Any:
    """Make context and condition values comparable (enums -> str, ISO str -> date)."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, list | tuple | set):
        return [_normalise(v) for v in value]
    return value


def condition_matches(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    field, op = condition.get("field"), condition.get("op")
    if op not in _OPERATORS:
        raise ApplicabilityError(f"Unsupported operator {op!r} in condition {dict(condition)}")
    if field not in context:
        raise ApplicabilityError(f"Context has no field {field!r} needed by {dict(condition)}")
    actual = _normalise(context[field])
    expected = _normalise(condition.get("value"))
    if actual is None and op not in ("eq", "ne"):
        return False
    try:
        return bool(_OPERATORS[op](actual, expected))
    except TypeError as exc:
        raise ApplicabilityError(f"Cannot compare {actual!r} {op} {expected!r}") from exc


def applies_to(conditions: list[Mapping[str, Any]], context: Mapping[str, Any]) -> bool:
    return all(condition_matches(c, context) for c in conditions)


def describe(conditions: list[Mapping[str, Any]]) -> str:
    """Readable form for traces, e.g. 'pf_applicable eq True AND ...'."""
    if not conditions:
        return "applies to all"
    return " AND ".join(f"{c['field']} {c['op']} {c['value']}" for c in conditions)
