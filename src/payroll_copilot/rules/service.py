"""Rule lookup for one employee and period: repository query + resolver."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import ESICRule, LWFRule, PFRule, PTRule, TDSRule
from payroll_copilot.domain.enums import RuleType
from payroll_copilot.rules.resolver import ResolvedRule, resolve_rule


class RuleService:
    def __init__(self, session: Session, allow_unverified: bool | None = None) -> None:
        self.session = session
        self.allow_unverified = (
            get_settings().allow_unverified_rules if allow_unverified is None else allow_unverified
        )

    def candidates(self, rule_type: RuleType, context: Mapping[str, Any]) -> list:
        """All stored versions that could apply, before date/applicability checks."""
        match rule_type:
            case RuleType.PF:
                stmt = select(PFRule)
            case RuleType.ESIC:
                stmt = select(ESICRule)
            case RuleType.PT:
                stmt = (
                    select(PTRule)
                    .where(PTRule.state_code == context["work_state"])
                    .options(selectinload(PTRule.slabs))
                )
            case RuleType.LWF:
                stmt = select(LWFRule).where(LWFRule.state_code == context["work_state"])
            case RuleType.TDS:
                stmt = (
                    select(TDSRule)
                    .where(
                        TDSRule.regime == context["tax_regime"],
                        or_(
                            TDSRule.age_category == context["age_category"],
                            TDSRule.age_category.is_(None),
                        ),
                    )
                    .options(selectinload(TDSRule.slabs))
                )
        return list(self.session.scalars(stmt))

    def resolve(
        self, rule_type: RuleType, period: PayrollPeriod, context: Mapping[str, Any]
    ) -> ResolvedRule:
        return resolve_rule(
            self.candidates(rule_type, context),
            period,
            context,
            rule_type=rule_type.value,
            allow_unverified=self.allow_unverified,
        )
