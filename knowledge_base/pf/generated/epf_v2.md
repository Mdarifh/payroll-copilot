# EPF ₹25,000 wage ceiling (September 2026)

- Rule: EPF version 2
- Effective: 2026-09-01 to open (no end date)
- Status: UNVERIFIED - not yet verified against the official source
- Applies when: pf_applicable eq True
- Source reference: Sept-2026 PF wage ceiling revision to ₹25,000 (as instructed by user) - attach notification
- Rounding: HALF_UP

> Generated from the payroll rule configuration used by the calculation engine. Do not edit by hand; run `payroll-copilot kb generate`.

## Contribution rates
- Employee EPF contribution: 12% of PF wage
- Employer contribution: 12% of PF wage, of which EPS 8.33% (on EPS wage) and the balance goes to Employer EPF
- EDLI: 0.5% of EDLI wage
- EPF administrative charges: 0.5% of PF wage (minimum ₹500 per establishment per month)

## Wage ceilings
- PF wage ceiling: ₹25000 per month
- EPS wage ceiling: ₹25000 per month
- EDLI wage ceiling: ₹25000 per month
- Ceiling proration by paid days: NONE
- EPS stops at age: 58

## How the engine applies this rule
- PF wage = min(earned PF wage, ceiling) when the employee's PF wage is restricted; otherwise the actual PF wage (EPS still capped at the EPS ceiling).
- Employee PF = PF wage × employee rate. EPS = min(PF wage, EPS ceiling) × EPS rate for EPS-eligible employees below the EPS age limit. Employer EPF = employer share − EPS.
- EDLI = min(PF wage, EDLI ceiling) × EDLI rate. Admin = PF wage × admin rate.
- The rule version is chosen by the payroll month; arrears use the rule of the month the arrear relates to.

## Notes
EPS and EDLI ceilings assumed to move to ₹25,000 along with the PF wage ceiling. Confirm from the official notification; if EPS stays at ₹15,000 change eps_wage_ceiling here (while UNVERIFIED) or add version 3.
