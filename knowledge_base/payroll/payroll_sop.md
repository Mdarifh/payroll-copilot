# Monthly payroll SOP (Demo Tech Pvt Ltd)

Standard operating procedure for running payroll in PayrollAI Copilot.

## 1. Pre-checks

- Open Payroll Runs, choose the month and run the pre-checks: salary structures,
  attendance, PF, ESIC, Professional Tax and TDS.
- Failed employee checks fail only those employees in the run; fix their data and use
  Retry failed. A company-level error (for example no PF rule for the month) blocks the run.

## 2. LOP review and calculation

- Start payroll. Review LOP for absences without approved leave, then run payroll.
- Employees already processed for the month are skipped. To redo a month, use
  Recalculate month (or roll back the batch) with a reason.

## 3. Changes during the month

- LOP, overtime, one-off earnings / deductions and salary revisions are changed from the
  Employees screen or AI Chat. Every change shows a preview first and needs a reason.
- With change approval on, a second HR user approves the change in the Action Center
  (maker-checker). Approval re-checks the numbers before applying.
- A change to a processed month recalculates the payslip, and a validated or approved
  month must be validated again.

## 4. Validate, audit, approve, finalize

- Validate the month and clear all errors. Run the AI Audit and resolve its findings
  (Fix recalculates a payslip).
- Approval must be given by a different HR user than the one who prepared the payroll.
- Finalize locks the payslips. A locked month cannot be changed or rolled back.

## 5. Statutory

- Check Compliance for the rule versions used, and the EPF challan totals for the month.
