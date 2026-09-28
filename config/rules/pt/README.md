# Professional Tax rules

One YAML file per state/UT (`rule_code: PT-<STATE>`).

**All slab values are UNVERIFIED.** They were entered from general knowledge as a
starting point and must be checked against each state's current PT Act /
notification before production use. `effective_from: 2025-04-01` is a seed
placeholder, not the notification date.

Slab semantics: a slab applies when `previous upto < wage <= upto`
(`upto: null` = no upper limit). `wage_basis` tells which income the limits refer
to (monthly, half-yearly or annual). `amount` is deducted in each of the
`deduction_months`; `month_overrides` replace the amount in a given month
(e.g. February ₹300 in Maharashtra).

States/UTs **without PT** use `is_levied: false` so the engine returns an explicit
"PT not levied" instead of a "no rule found" error.

Not yet configured (confirm whether PT applies and add a file): Goa, Chhattisgarh,
Arunachal Pradesh, Jammu & Kashmir, Ladakh, Andaman & Nicobar, Dadra & Nagar
Haveli and Daman & Diu, Lakshadweep.
