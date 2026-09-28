# ESIC 0.75% / 3.25%, wage threshold ₹21,000

- Rule: ESIC version 1
- Effective: 2019-07-01 to open (no end date)
- Status: UNVERIFIED - not yet verified against the official source
- Applies when: esic_applicable eq True
- Source reference: ESI (Central) Rules - rates 0.75%/3.25% w.e.f. 01-07-2019; threshold ₹21,000 (₹25,000 for PwD) w.e.f. 01-01-2017
- Rounding: CEIL

> Generated from the payroll rule configuration used by the calculation engine. Do not edit by hand; run `payroll-copilot kb generate`.

## Contribution rates
- Employee ESIC: 0.75% of ESIC wage
- Employer ESIC: 3.25% of ESIC wage

## Coverage
- Wage threshold: ₹21000 per month (₹25000 for employees with disability)
- Contribution periods start in months [4, 10]; coverage is decided at the start of the contribution period (or on joining) using the full-month ESIC wage rate, and continues until the period ends.
- Arrears paid in a month count as ESIC wage of that month but are not used for the coverage test.

## Notes
Coverage is decided at the start of each contribution period (Apr-Sep, Oct-Mar); an employee covered at period start stays covered until the period ends even if wages cross the threshold. Contributions rounded up to the next rupee.
