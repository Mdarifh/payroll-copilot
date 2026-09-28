"""Pick exactly one rule version for an employee and payroll period.

The resolver never guesses: zero matches, overlapping matches and mid-period
rule changes are all explicit errors the caller has to handle.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from typing import Any, Protocol

from payroll_copilot.core.dates import PayrollPeriod, date_ranges_overlap
from payroll_copilot.core.exceptions import (
    AmbiguousRuleError,
    MidPeriodRuleChangeError,
    RuleNotFoundError,
    UnverifiedRuleError,
)
from payroll_copilot.domain.enums import RuleStatus
from payroll_copilot.rules.applicability import applies_to, describe


class RuleVersionLike(Protocol):
    rule_code: str
    version: int
    effective_from: date
    effective_to: date | None
    status: RuleStatus
    applicability: list


@dataclass(frozen=True)
class ResolvedRule[T: RuleVersionLike]:
    rule: T
    warnings: list[str] = field(default_factory=list)

    @property
    def reference(self) -> str:
        r = self.rule
        until = r.effective_to.isoformat() if r.effective_to else "open"
        return f"{r.rule_code} v{r.version} ({r.effective_from.isoformat()} to {until})"


def _label(rule: RuleVersionLike) -> str:
    return f"{rule.rule_code} v{rule.version}"


def resolve_rule[T: RuleVersionLike](
    candidates: Sequence[T],
    period: PayrollPeriod,
    context: Mapping[str, Any],
    *,
    rule_type: str,
    allow_unverified: bool = True,
) -> ResolvedRule[T]:
    who = context.get("employee_code", "<unknown employee>")
    in_period = [
        r
        for r in candidates
        if r.status is not RuleStatus.RETIRED
        and date_ranges_overlap(r.effective_from, r.effective_to, period.start, period.end)
    ]
    matches = [r for r in in_period if applies_to(r.applicability, context)]

    if not matches:
        detail = (
            "no version is effective in this period"
            if not in_period
            else "effective versions exist but applicability did not match: "
            + "; ".join(f"{_label(r)} [{describe(r.applicability)}]" for r in in_period)
        )
        raise RuleNotFoundError(f"No {rule_type} rule for {who} in {period}: {detail}")

    if len(matches) > 1:
        names = ", ".join(_label(r) for r in matches)
        overlapping = any(
            date_ranges_overlap(a.effective_from, a.effective_to, b.effective_from, b.effective_to)
            for a, b in combinations(matches, 2)
        )
        if overlapping:
            raise AmbiguousRuleError(
                f"{len(matches)} {rule_type} rules apply to {who} in {period}: {names}"
            )
        raise MidPeriodRuleChangeError(
            f"{rule_type} rule changes within {period} for {who} ({names}); split the period",
            rules=sorted(matches, key=lambda r: r.effective_from),
        )

    rule = matches[0]
    warnings: list[str] = []
    if rule.effective_from > period.start or (
        rule.effective_to is not None and rule.effective_to < period.end
    ):
        warnings.append(f"{_label(rule)} covers only part of {period}")
    if rule.status is RuleStatus.UNVERIFIED:
        if not allow_unverified:
            raise UnverifiedRuleError(
                f"{_label(rule)} is UNVERIFIED and ALLOW_UNVERIFIED_RULES is false"
            )
        warnings.append(f"{_label(rule)} is UNVERIFIED - verify against the official source")
    return ResolvedRule(rule=rule, warnings=warnings)
