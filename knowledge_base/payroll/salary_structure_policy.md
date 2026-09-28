# Salary structure and proration policy (Demo Tech Pvt Ltd)

Demo company policy describing how the engine builds and prorates salary.

## Structure templates

- STD-2026: Basic 50% of monthly gross, HRA 50% of Basic, Conveyance ₹1,600,
  Medical ₹1,250, Special Allowance = balancing amount.
- LOW-2026: Basic 60% of monthly gross, HRA 40% of Basic, Special Allowance =
  balancing amount.
- Each component is rounded individually; the balancing head absorbs rounding so
  the monthly gross never changes.

## Buckets

- Earnings add up to gross earnings.
- Employee deductions (PF, ESIC, PT, LWF, TDS, loans) reduce net pay.
- Employer contributions (Employer EPF, EPS, EDLI, EPF admin, Employer ESIC,
  Employer LWF) never reduce net pay; they add to cost to company (CTC).
- Net pay = gross earnings − employee deductions.

## Proration

- A prorated head is paid as full-month amount × paid days / basis days and
  rounded per line.
- When salary changes within a month, each structure segment is prorated
  separately. LOP is allocated to the latest segment first (LAST_SEGMENT).
- Wage bases for statutory calculations come from head flags: PF wage (Basic in
  the demo structures), ESIC wage, PT wage, LWF wage and taxable earnings.
