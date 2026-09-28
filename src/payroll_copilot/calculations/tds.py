"""TDS on salary: projected annual tax and this month's deduction.

All slabs, rebate, surcharge, cess, standard deduction, deduction limits and
whether PT is deductible come from the resolved TDS rule (FY + regime + age).

    gross taxable salary = Σ monthly taxable earnings for the FY (actual/estimated/projected)
    taxable income       = gross − standard deduction − PT (if allowed) − section deductions
    slab tax             = Σ (income in slab × slab rate)
    rebate               = min(slab tax, rebate_max) if income ≤ rebate limit
                           (marginal relief: tax ≤ income − rebate limit, if the rule allows)
    surcharge            = tax × rate for the highest threshold crossed, with marginal relief
    cess                 = (tax + surcharge) × cess rate
    monthly TDS          = (annual tax − TDS already deducted) / remaining employed months
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult, RuleRef, TraceStep
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RUPEE, round_money, to_decimal
from payroll_copilot.domain.enums import RuleType
from payroll_copilot.domain.salary import PayLine

ZERO = Decimal(0)


class AnnualTax(BaseModel):
    model_config = ConfigDict(frozen=True)

    taxable_income: Decimal
    slab_tax: Decimal
    slab_breakdown: dict[str, Decimal]
    rebate: Decimal
    tax_after_rebate: Decimal
    surcharge: Decimal
    surcharge_rate: Decimal
    cess: Decimal
    total_tax: Decimal
    notes: list[str] = Field(default_factory=list)


def _slab_tax(income: Decimal, rule: Any) -> tuple[Decimal, dict[str, Decimal]]:
    total, breakdown = ZERO, {}
    for slab in sorted(rule.slabs, key=lambda s: s.sequence):
        lower = slab.lower_exclusive or ZERO
        if income <= lower:
            break
        upper = income if slab.upper_inclusive is None else min(income, slab.upper_inclusive)
        portion = (upper - lower) * slab.rate / 100
        label = f"{lower}-{slab.upper_inclusive or '∞'} @ {slab.rate}%"
        breakdown[label] = portion
        total += portion
    return total, breakdown


def _tax_before_surcharge(income: Decimal, rule: Any) -> tuple[Decimal, Decimal, dict, list[str]]:
    """Slab tax, rebate and marginal relief on rebate."""
    notes: list[str] = []
    slab_tax, breakdown = _slab_tax(income, rule)
    rebate = ZERO
    limit = rule.rebate_income_limit
    if limit is not None and income <= limit:
        rebate = min(slab_tax, rule.rebate_max or ZERO)
    elif limit is not None and rule.rebate_marginal_relief and slab_tax > income - limit:
        rebate = slab_tax - (income - limit)
        notes.append(f"marginal relief on rebate: tax limited to income above {limit}")
    return slab_tax, rebate, breakdown, notes


def compute_annual_tax(taxable_income: Decimal | int | str, rule: Any) -> AnnualTax:
    income = max(to_decimal(taxable_income), ZERO)
    slab_tax, rebate, breakdown, notes = _tax_before_surcharge(income, rule)
    tax = slab_tax - rebate

    surcharge, rate = ZERO, ZERO
    crossed = [s for s in rule.surcharge if income > to_decimal(s["above"])]
    if crossed and tax > 0:
        top = max(crossed, key=lambda s: to_decimal(s["above"]))
        threshold, rate = to_decimal(top["above"]), to_decimal(top["rate"])
        surcharge = tax * rate / 100
        # marginal relief: tax + surcharge may not exceed tax at the threshold
        # (with the lower surcharge rate) + income above the threshold
        lower = [s for s in rule.surcharge if to_decimal(s["above"]) < threshold]
        lower_rate = max((to_decimal(s["rate"]) for s in lower), default=ZERO)
        base_tax, base_rebate, _, _ = _tax_before_surcharge(threshold, rule)
        at_threshold = (base_tax - base_rebate) * (1 + lower_rate / 100)
        cap = at_threshold + (income - threshold)
        if tax + surcharge > cap:
            surcharge = max(cap - tax, ZERO)
            notes.append(f"marginal relief on surcharge above {threshold}")

    cess = (tax + surcharge) * rule.cess_rate / 100
    total = round_money(tax + surcharge + cess, rule.rounding_mode, RUPEE)
    return AnnualTax(
        taxable_income=income, slab_tax=slab_tax, slab_breakdown=breakdown, rebate=rebate,
        tax_after_rebate=tax, surcharge=surcharge, surcharge_rate=rate, cess=cess,
        total_tax=total, notes=notes,
    )  # fmt: skip


class Declaration(BaseModel):
    model_config = ConfigDict(frozen=True)

    section_code: str
    amount: Decimal


class TDSInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    period: str
    monthly_taxable: dict[str, Decimal]  # "2026-04 (estimated from structure)": amount
    professional_tax_annual: Decimal = ZERO
    declarations: list[Declaration] = Field(default_factory=list)
    tds_already_deducted: Decimal = ZERO
    tds_history_note: str = ""
    remaining_months: int
    override_amount: Decimal | None = None
    override_reason: str | None = None


class TDSResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    applicable: bool = True
    configured: bool = True
    reason: str | None = None
    rule: RuleRef | None = None
    gross_taxable_salary: Decimal = ZERO
    standard_deduction: Decimal = ZERO
    professional_tax: Decimal = ZERO
    section_deductions: dict[str, Decimal] = Field(default_factory=dict)
    annual: AnnualTax | None = None
    tds_already_deducted: Decimal = ZERO
    remaining_months: int = 0
    computed_monthly_tds: Decimal = ZERO
    monthly_tds: Decimal = ZERO
    overridden: bool = False
    trace: CalculationResult | None = None
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def not_applicable(cls, reason: str) -> "TDSResult":
        return cls(applicable=False, reason=reason)

    @classmethod
    def not_configured(cls, reason: str) -> "TDSResult":
        return cls(applicable=True, configured=False, reason=reason)

    def deductions(self) -> list[PayLine]:
        if not (self.applicable and self.configured):
            return []
        return [PayLine(head_code="TDS", amount=self.monthly_tds)]


def calculate_tds(inp: TDSInput, rule: Any, rule_warnings: list[str] | None = None) -> TDSResult:
    if inp.remaining_months < 1:
        raise CalculationError("remaining_months must be at least 1 (current month)")
    warnings = list(rule_warnings or [])
    steps: list[TraceStep] = []
    ref = RuleRef.from_rule(rule, RuleType.TDS)

    gross = sum(inp.monthly_taxable.values(), ZERO)
    steps.append(TraceStep(
        label="Gross taxable salary (FY)",
        formula=" + ".join(f"{k} {v}" for k, v in inp.monthly_taxable.items()),
        values=dict(inp.monthly_taxable), result=gross,
    ))  # fmt: skip

    std = min(rule.standard_deduction, gross)
    steps.append(
        TraceStep(
            label="Standard deduction",
            formula=f"min({rule.standard_deduction}, gross)",
            result=std,
        )
    )

    pt = inp.professional_tax_annual if rule.allow_professional_tax_deduction else ZERO
    if inp.professional_tax_annual and not rule.allow_professional_tax_deduction:
        warnings.append(f"PT not deductible under {rule.regime.value} regime rule; ignored")
    if pt:
        steps.append(TraceStep(label="Professional Tax (projected)", result=pt))

    limits = {k: to_decimal(v) for k, v in (rule.deduction_limits or {}).items()}
    sections: dict[str, Decimal] = {}
    for decl in inp.declarations:
        if decl.section_code not in limits:
            warnings.append(
                f"Declaration {decl.section_code} ₹{decl.amount} ignored: not allowed by "
                f"{ref.rule_code} ({rule.regime.value} regime)"
            )
            continue
        limit = limits[decl.section_code]
        allowed = min(decl.amount, limit)
        sections[decl.section_code] = sections.get(decl.section_code, ZERO) + allowed
        steps.append(
            TraceStep(
                label=f"Deduction {decl.section_code}",
                formula=f"min(declared {decl.amount}, limit {limit})",
                result=allowed,
            )
        )
    for code, value in sections.items():  # combined declarations still capped
        sections[code] = min(value, limits[code])

    taxable = max(gross - std - pt - sum(sections.values(), ZERO), ZERO)
    steps.append(TraceStep(label="Taxable income", formula="gross − deductions", result=taxable))

    annual = compute_annual_tax(taxable, rule)
    for label, amount in annual.slab_breakdown.items():
        steps.append(TraceStep(label=f"Slab {label}", result=amount))
    if annual.rebate:
        within_limit = taxable <= (rule.rebate_income_limit or ZERO)
        rebate_formula = (
            f"income ≤ {rule.rebate_income_limit}" if within_limit else "marginal relief"
        )
        steps.append(TraceStep(label="Rebate", formula=rebate_formula, result=annual.rebate))
    if annual.surcharge:
        steps.append(
            TraceStep(label=f"Surcharge @ {annual.surcharge_rate}%", result=annual.surcharge)
        )
    steps.append(TraceStep(label=f"Cess @ {rule.cess_rate}%", result=annual.cess))
    steps.append(TraceStep(label="Annual tax", result=annual.total_tax))
    warnings.extend(annual.notes)

    balance = max(annual.total_tax - inp.tds_already_deducted, ZERO)
    raw_monthly = balance / inp.remaining_months
    monthly = round_money(raw_monthly, rule.rounding_mode, RUPEE)
    steps.append(TraceStep(
        label="TDS already deducted this FY", result=inp.tds_already_deducted,
        note=inp.tds_history_note or None,
    ))  # fmt: skip
    monthly_formula = (
        f"({annual.total_tax} − {inp.tds_already_deducted}) / {inp.remaining_months} months"
    )
    steps.append(TraceStep(label="Monthly TDS", formula=monthly_formula, result=monthly))
    if inp.tds_already_deducted > annual.total_tax:
        warnings.append("TDS already deducted exceeds projected annual tax; no further TDS")

    final, overridden = monthly, False
    if inp.override_amount is not None:
        final, overridden = to_decimal(inp.override_amount), True
        steps.append(TraceStep(label="Manual override", result=final,
                               note=inp.override_reason or "no reason given"))  # fmt: skip
        warnings.append(f"TDS manually overridden: computed {monthly}, used {final}")

    trace = CalculationResult(
        component="TDS", employee_code=inp.employee_code, period=inp.period,
        inputs={"declarations": [d.model_dump() for d in inp.declarations],
                "remaining_months": inp.remaining_months,
                "tds_already_deducted": inp.tds_already_deducted},
        rule=ref,
        formula="(annual tax on projected taxable salary − TDS so far) / remaining months",
        steps=steps, raw_value=raw_monthly, value=final, rounding_mode=rule.rounding_mode,
        warnings=warnings,
    )  # fmt: skip
    return TDSResult(
        rule=ref, gross_taxable_salary=gross, standard_deduction=std, professional_tax=pt,
        section_deductions=sections, annual=annual, tds_already_deducted=inp.tds_already_deducted,
        remaining_months=inp.remaining_months, computed_monthly_tds=monthly, monthly_tds=final,
        overridden=overridden, trace=trace, warnings=warnings,
    )  # fmt: skip
