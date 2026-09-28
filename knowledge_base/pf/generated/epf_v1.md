# EPF ₹15,000 wage ceiling (admin 0.50%)

- Rule: EPF version 1
- Effective: 2018-06-01 to 2026-08-31
- Status: UNVERIFIED - not yet verified against the official source
- Applies when: pf_applicable eq True
- Source reference: EPF Scheme 1952 / EPS 1995 (ceiling ₹15,000 w.e.f. 01-09-2014); admin charge 0.50% w.e.f. 01-06-2018
- Rounding: HALF_UP

> Generated from the payroll rule configuration used by the calculation engine. Do not edit by hand; run `payroll-copilot kb generate`.

## Contribution rates
- Employee EPF contribution: 12% of PF wage
- Employer contribution: 12% of PF wage, of which EPS 8.33% (on EPS wage) and the balance goes to Employer EPF
- EDLI: 0.5% of EDLI wage
- EPF administrative charges: 0.5% of PF wage (minimum ₹500 per establishment per month)

## Wage ceilings
- PF wage ceiling: ₹15000 per month
- EPS wage ceiling: ₹15000 per month
- EDLI wage ceiling: ₹15000 per month
- Ceiling proration by paid days: NONE
- EPS stops at age: 58

## How the engine applies this rule
- PF wage = min(earned PF wage, ceiling) when the employee's PF wage is restricted; otherwise the actual PF wage (EPS still capped at the EPS ceiling).
- Employee PF = PF wage × employee rate. EPS = min(PF wage, EPS ceiling) × EPS rate for EPS-eligible employees below the EPS age limit. Employer EPF = employer share − EPS.
- EDLI = min(PF wage, EDLI ceiling) × EDLI rate. Admin = PF wage × admin rate.
- The rule version is chosen by the payroll month; arrears use the rule of the month the arrear relates to.
