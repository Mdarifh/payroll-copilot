# Labour Welfare Fund rules

One YAML file per state (`rule_code: LWF-<STATE>`). **All values are UNVERIFIED**
and `effective_from: 2025-04-01` is a seed placeholder. Only states used by the
seed employees are configured so far; a missing state means "LWF rule not
configured" (the engine will warn, not assume zero).

Fixed-amount states set `employee_amount` + `employer_amount`; percentage states
set `employee_rate` (+ `employee_max_amount`) and `employer_multiplier`.
