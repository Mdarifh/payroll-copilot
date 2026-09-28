"""Generate knowledge-base documents from the versioned rule configuration.

Rule documents are generated (not hand-written) so the policy assistant always
says exactly what the calculation engine uses - same values, same versions,
same effective dates, same verification status.
"""

from pathlib import Path

import yaml

from payroll_copilot.rules.applicability import describe
from payroll_copilot.rules.schemas import (
    ESICRuleFile,
    LWFRuleFile,
    PFRuleFile,
    PTRuleFile,
    RuleFileBase,
    TDSRuleFile,
)

GENERATED_DIR = "generated"
_MODULE = {PFRuleFile: "pf", ESICRuleFile: "esic", PTRuleFile: "pt", LWFRuleFile: "lwf",
           TDSRuleFile: "tds"}  # fmt: skip


def _header(rule_file: RuleFileBase, v) -> list[str]:
    until = v.effective_to.isoformat() if v.effective_to else "open (no end date)"
    lines = [
        f"# {v.name}",
        "",
        f"- Rule: {rule_file.rule_code} version {v.version}",
        f"- Effective: {v.effective_from.isoformat()} to {until}",
        f"- Status: {v.status.value}"
        + (" - not yet verified against the official source" if v.status.value != "VERIFIED"
           else ""),
        f"- Applies when: {describe([c.model_dump() for c in v.applicability])}",
        f"- Source reference: {v.source_ref or 'not recorded'}",
        f"- Rounding: {v.rounding_mode.value}",
        "",
        "> Generated from the payroll rule configuration used by the calculation engine. "
        "Do not edit by hand; run `payroll-copilot kb generate`.",
        "",
    ]  # fmt: skip
    return lines


def _slabs(slabs, value_label: str, suffix: str = "") -> list[str]:
    lines, lower = [], None
    for s in slabs:
        low = "0" if lower is None else f"above {lower}"
        high = f"up to {s.upto}" if s.upto is not None else "and above"
        value = getattr(s, "amount", None)
        value = value if value is not None else s.rate
        extra = ""
        overrides = getattr(s, "month_overrides", None)
        if overrides:
            extra = " (month overrides: " + ", ".join(f"month {m}: ₹{a}" for m, a in
                                                        overrides.items()) + ")"  # fmt: skip
        lines.append(f"| {low} {high} | {value}{suffix}{extra} |")
        lower = s.upto
    return [f"| Wage / income band | {value_label} |", "|---|---|", *lines]


def _pf(v) -> list[str]:
    return [
        "## Contribution rates",
        f"- Employee EPF contribution: {v.employee_rate}% of PF wage",
        f"- Employer contribution: {v.employer_rate}% of PF wage, of which EPS {v.eps_rate}%"
        " (on EPS wage) and the balance goes to Employer EPF",
        f"- EDLI: {v.edli_rate}% of EDLI wage",
        f"- EPF administrative charges: {v.admin_rate}% of PF wage"
        + (f" (minimum ₹{v.admin_min_amount} per establishment per month)"
           if v.admin_min_amount else ""),
        "",
        "## Wage ceilings",
        f"- PF wage ceiling: ₹{v.pf_wage_ceiling} per month",
        f"- EPS wage ceiling: ₹{v.eps_wage_ceiling} per month",
        f"- EDLI wage ceiling: ₹{v.edli_wage_ceiling} per month",
        f"- Ceiling proration by paid days: {v.ceiling_proration.value}",
        f"- EPS stops at age: {v.eps_max_age or 'no age limit configured'}",
        "",
        "## How the engine applies this rule",
        "- PF wage = min(earned PF wage, ceiling) when the employee's PF wage is restricted;"
        " otherwise the actual PF wage (EPS still capped at the EPS ceiling).",
        "- Employee PF = PF wage × employee rate. EPS = min(PF wage, EPS ceiling) × EPS rate"
        " for EPS-eligible employees below the EPS age limit. Employer EPF = employer share − EPS.",
        "- EDLI = min(PF wage, EDLI ceiling) × EDLI rate. Admin = PF wage × admin rate.",
        "- The rule version is chosen by the payroll month; arrears use the rule of the"
        " month the arrear relates to.",
    ]  # fmt: skip


def _esic(v) -> list[str]:
    return [
        "## Contribution rates",
        f"- Employee ESIC: {v.employee_rate}% of ESIC wage",
        f"- Employer ESIC: {v.employer_rate}% of ESIC wage",
        "",
        "## Coverage",
        f"- Wage threshold: ₹{v.wage_threshold} per month"
        + (f" (₹{v.disabled_wage_threshold} for employees with disability)"
           if v.disabled_wage_threshold else ""),
        f"- Contribution periods start in months {v.contribution_period_start_months};"
        " coverage is decided at the start of the contribution period (or on joining) using"
        " the full-month ESIC wage rate, and continues until the period ends.",
        "- Arrears paid in a month count as ESIC wage of that month but are not used for"
        " the coverage test.",
    ]  # fmt: skip


def _pt(rule_file, v) -> list[str]:
    if not v.is_levied:
        return ["## Levy", f"- Professional Tax is not levied in {rule_file.state_name}."]
    lines = [
        "## Levy",
        f"- State: {rule_file.state_name} ({rule_file.state_code})",
        f"- Frequency: {v.frequency.value}; deducted in months {v.effective_deduction_months}",
        f"- Slabs are applied to {v.wage_basis.value.lower().replace('_', '-')} income",
        "",
    ]
    for slab_set in v.slab_sets:
        who = f"gender {slab_set.gender.value}" if slab_set.gender else "all other employees"
        lines += [f"## Slabs ({who})", "", *_slabs(slab_set.slabs, "PT amount (₹)"), ""]
    return lines


def _lwf(rule_file, v) -> list[str]:
    lines = [
        "## Contribution",
        f"- State: {rule_file.state_name} ({rule_file.state_code})",
        f"- Frequency: {v.frequency.value}; deducted in months {v.deduction_months}",
    ]
    if v.employee_amount is not None:
        lines += [f"- Employee: ₹{v.employee_amount}", f"- Employer: ₹{v.employer_amount}"]
    else:
        cap = f", maximum ₹{v.employee_max_amount}" if v.employee_max_amount else ""
        lines += [f"- Employee: {v.employee_rate}% of LWF wage{cap}",
                  f"- Employer: {v.employer_multiplier} × employee contribution"]  # fmt: skip
    if v.wage_upper_limit:
        lines.append(f"- Applies only when LWF wage ≤ ₹{v.wage_upper_limit}")
    return lines


def _tds(v) -> list[str]:
    age = v.age_category.value if v.age_category else "all ages"
    lines = [
        "## Scope",
        f"- Financial year {v.financial_year}, {v.regime.value} tax regime, {age}",
        "",
        "## Deductions and relief",
        f"- Standard deduction: ₹{v.standard_deduction}",
        f"- Rebate: up to ₹{v.rebate_max} when taxable income ≤ ₹{v.rebate_income_limit}"
        + (" (with marginal relief above the limit)" if v.rebate_marginal_relief else "")
        if v.rebate_income_limit else "- Rebate: none configured",
        f"- Health and education cess: {v.cess_rate}%",
        "- Professional Tax deductible: " + ("yes" if v.allow_professional_tax_deduction
                                              else "no"),
        "- Section deduction limits: " + (", ".join(f"{k} ₹{a}" for k, a in
                                                    v.deduction_limits.items())
                                          or "none (declarations not allowed)"),
        "",
        "## Tax slabs",
        "",
        *_slabs(v.slabs, "Rate", "%"),
        "",
        "## Surcharge",
        *([f"- Income above ₹{s.above}: {s.rate}%" for s in v.surcharge] or ["- none"]),
        "",
        "## Monthly TDS",
        "- Monthly TDS = (projected annual tax − TDS already deducted) / remaining months"
        " of employment in the financial year.",
    ]  # fmt: skip
    return lines


def render(rule_file: RuleFileBase, version) -> str:
    body = {
        PFRuleFile: lambda: _pf(version),
        ESICRuleFile: lambda: _esic(version),
        PTRuleFile: lambda: _pt(rule_file, version),
        LWFRuleFile: lambda: _lwf(rule_file, version),
        TDSRuleFile: lambda: _tds(version),
    }[type(rule_file)]()
    notes = ["", "## Notes", version.notes] if version.notes else []
    return "\n".join([*_header(rule_file, version), *body, *notes]) + "\n"


def metadata(rule_file: RuleFileBase, version) -> dict:
    state = getattr(rule_file, "state_code", None)
    return {
        "module": _MODULE[type(rule_file)],
        "document_name": f"{rule_file.rule_code} v{version.version}",
        "rule_name": rule_file.rule_code,
        "version": str(version.version),
        "effective_from": version.effective_from.isoformat(),
        "effective_to": version.effective_to.isoformat() if version.effective_to else None,
        "jurisdiction": f"IN-{state}" if state else "IN",
        "source": version.source_ref or "payroll rule configuration",
        "status": version.status.value,
        "doc_type": "rule",
    }


def generate_rule_documents(rule_files: list[RuleFileBase], kb_root: Path) -> list[Path]:
    """Write <module>/generated/<rule>_v<n>.md + .meta.yaml; stale files are removed."""
    written: list[Path] = []
    for module in set(_MODULE.values()):
        folder = kb_root / module / GENERATED_DIR
        if folder.exists():
            for old in folder.iterdir():
                old.unlink()
    for rule_file in rule_files:
        module = _MODULE[type(rule_file)]
        folder = kb_root / module / GENERATED_DIR
        folder.mkdir(parents=True, exist_ok=True)
        for version in rule_file.versions:
            path = folder / f"{rule_file.rule_code.lower()}_v{version.version}.md"
            path.write_text(render(rule_file, version), encoding="utf-8")
            meta_path = path.with_name(path.name + ".meta.yaml")
            meta_path.write_text(
                yaml.safe_dump(metadata(rule_file, version), sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            written.append(path)
    return written
