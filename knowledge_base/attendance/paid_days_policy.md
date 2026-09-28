# Paid days and LOP policy (Demo Tech Pvt Ltd)

This is the demo company's configured policy. It describes how the payroll
engine counts paid days; it is not a statement of law.

## Paid-days basis

- Company default basis: CALENDAR (days in the month). An employee-level basis
  overrides the company default.
- Paid days = payable days in the employed span − LOP days.
- Employed span runs from the later of the month start and the date of joining
  (DOJ) to the earlier of the month end and the last working day (LWD).
- On a FIXED_30 or FIXED_26 basis a full month is always 30 or 26 paid days. For
  a partial month the engine prorates the basis by employed calendar days and
  shows a warning so the policy can be confirmed.

## LOP (loss of pay)

- LOP days come from the attendance summary. Half days are allowed.
- LOP cannot exceed the payable days of the month.
- LOP reversal (LOP wrongly deducted in an earlier month) is not added to the
  current month's paid days. It is paid as a separate arrear at the rate of the
  month in which the LOP was deducted.

## Why paid days can differ

Typical reasons the engine checks when payroll shows different paid days: LOP
not deducted or deducted twice, days before DOJ or after LWD not excluded, LOP
reversal added to paid days, week offs or holidays treated as unpaid, or a
different paid-days basis used by the payroll system.
