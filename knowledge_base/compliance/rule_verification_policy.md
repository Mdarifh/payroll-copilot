# Rule verification and versioning policy

How statutory rules are maintained in the AI Payroll Copilot.

## Versioning

- Every statutory rule (PF, ESIC, PT, LWF, TDS) is a versioned configuration
  with effective dates. The engine picks exactly one version per employee and
  payroll month and never guesses when versions overlap.
- A new notification is added as a new version; verified versions are never
  edited in place.

## Verification status

- UNVERIFIED: entered from general knowledge or an instruction and not yet
  checked against the official notification. Calculations still run but every
  trace carries a warning.
- VERIFIED: checked against the official source.
- RETIRED: kept for history, never used.

## Current state

- All statutory rules in this demo are UNVERIFIED and must be checked by the
  payroll compliance team before production use.
