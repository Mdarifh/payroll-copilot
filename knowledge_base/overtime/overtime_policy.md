# Overtime policy (Demo Tech Pvt Ltd)

Demo company policy matching config/payroll/overtime.yaml. HR must confirm these values
before live use. It is not a statement of law.

## Overtime rate

- If an agreed overtime rate is set for the employee, that rate is used.
- Otherwise the company policy applies: ordinary hourly rate = monthly Basic ÷ 26 days ÷
  8 hours, and the overtime rate is twice the ordinary hourly rate. This follows the
  Factories Act, 1948 section 59 principle of overtime at twice the ordinary rate of
  wages; which heads make up the "ordinary rate" is a company decision (Basic only here).
- The rate is rounded to the paisa, and the overtime amount (hours × rate) is rounded to
  the rupee. Example: Basic ₹26,000 gives ₹125 per hour ordinary rate and ₹250 per hour
  overtime; 6 hours = ₹1,500.
- The AI assistant never decides an overtime rate. If no rate can be found, overtime
  cannot be added.

## Limits

- At most 8 overtime hours on one day and 50 overtime hours in one payroll month
  (company limits; check the state Factories Rules for the legal limits).
- Overtime cannot be recorded for a future date or a day the employee was not employed.

## Payroll treatment

- Overtime is paid in the payroll month it is recorded for, as the "Overtime" earning.
- It is taxable and counts for Professional Tax and for the ESIC contribution, but not
  for PF wages and not for deciding ESIC coverage.
- The rate and amount are fixed when the entry is saved, so a later salary change does
  not change past overtime.
