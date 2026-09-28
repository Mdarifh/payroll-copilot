"""Deterministic root-cause helpers for expected-vs-actual mismatches.

The LLM never guesses causes: every candidate cause here is backed by a
recomputation that reproduces the actual value (or an arithmetic identity).
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal

from payroll_copilot.calculations.validation import ComparisonResult, ComponentGroup

ZERO = Decimal(0)


def is_rounding(difference: Decimal | None, limit: Decimal = Decimal(1)) -> bool:
    return difference is not None and difference != 0 and abs(difference) <= limit


def matching_candidates(
    actual: Decimal | None, candidates: Mapping[str, Decimal], tolerance: Decimal = ZERO
) -> list[str]:
    """Labels of alternative calculations that reproduce the actual value."""
    if actual is None:
        return []
    return [label for label, value in candidates.items() if abs(value - actual) <= tolerance]


def _diff(results: Iterable[ComparisonResult], component: str) -> Decimal:
    for r in results:
        if r.component == component and r.difference is not None:
            return r.difference
    return ZERO


def explain_net(results: list[ComparisonResult]) -> str | None:
    """Net diff = gross diff − Σ deduction diffs, when the arithmetic reconciles."""
    net = _diff(results, "NET_PAY")
    if net == 0:
        return None
    gross = _diff(results, "GROSS")
    deductions = [
        r for r in results
        if r.group in (ComponentGroup.PF, ComponentGroup.ESIC, ComponentGroup.PT,
                       ComponentGroup.LWF, ComponentGroup.TDS, ComponentGroup.OTHER_DEDUCTION)
        and r.component.startswith(("EE_", "PT", "TDS", "LWF_EE", "LOAN", "OTHER_DED", "NOTICE"))
        and r.difference
    ]  # fmt: skip
    ded_total = sum((r.difference for r in deductions), ZERO)
    if gross - ded_total != net:
        return None
    parts = [f"gross {gross:+}"] if gross else []
    parts += [f"{r.component} {-r.difference:+}" for r in deductions]
    return "Net difference is fully explained by: " + ", ".join(parts)
