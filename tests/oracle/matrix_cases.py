"""The PF test-matrix cases (same definitions that produced the approved plan's matrix).

Each case: (id, group, description, oracle Employee, month, edli_basis). Expected values are
never typed by hand: tests compare the engine with the independent oracle, and the oracle is
itself pinned to EPFO's published illustrations (test_oracle_matches_epfo_faq).
"""

from datetime import date
from decimal import Decimal as D

from tests.oracle.pf_oracle import Employee

SEP = (date(2026, 9, 1), date(2026, 9, 30))
AUG = (date(2026, 8, 1), date(2026, 8, 31))
OCT = (date(2026, 10, 1), date(2026, 10, 31))
MONTHS = {"2026-09": SEP, "2026-08": AUG, "2026-10": OCT}


def d(day, m=9):
    return date(2026, m, day)


CASES: list[tuple] = []


def add(cid, group, desc, emp, month="2026-09", edli_basis="pf_wage", note=""):
    CASES.append((cid, group, desc, emp, month, edli_basis))


# ---------------------------------------------------------------- spec examples (section 15)
add("EX-A", "Spec examples", "Joined before September; PF from DOJ 10-Aug", Employee(D(30000), d(10, 8), d(10, 8)))
add("EX-B", "Spec examples", "Joins 01-Sep", Employee(D(30000), d(1), d(1)))
add("EX-C", "Spec examples", "Joins 17-Sep: NEW only, no OLD contribution", Employee(D(30000), d(17), d(17)))
add("EX-D", "Spec examples", "Joins 25-Sep: only 25–30 Sep", Employee(D(30000), d(25), d(25)))
add("EX-E", "Spec examples", "DOJ 01-Aug, PF applicable 20-Sep (DOJ alone is not enough)", Employee(D(30000), d(1, 8), d(20)))
add("EX-F", "Spec examples", "PF applicable date before 17-Sep (05-Sep)", Employee(D(30000), d(1, 8), d(5)))
add("EX-G", "Spec examples", "PF applicable date exactly 17-Sep", Employee(D(30000), d(1, 8), d(17)))
add("EX-H", "Spec examples", "Exit 20-Sep crosses the boundary", Employee(D(30000), exit=d(20)))
add("EX-LOP", "Spec examples", "LOP 15, 16, 17 Sep crosses the boundary (section 16), ₹15,000 wage", Employee(D(15000), lop=(d(15), d(16), d(17))))
add("EX-LOP-30K", "Spec examples", "Same LOP at ₹30,000: ceiling masks LOP (no PF change)", Employee(D(30000), lop=(d(15), d(16), d(17))))
add("EX-REV", "Spec examples", "Revision 20-Sep + rule 17-Sep → 3 segments (section 17)", Employee(D(20000), revisions=((d(20), D(30000)),)))

# ---------------------------------------------------------------- EPFO official illustrations (golden)
add("FAQ-Q7A", "EPFO FAQ golden", "Excluded employee ₹20,000 becomes member from 17-Sep", Employee(D(20000), d(1, 1), d(17)))
add("FAQ-Q7B", "EPFO FAQ golden", "EPF-only member on actual ₹20,000; EPS from 17-Sep", Employee(D(20000), policy="actual_wages", eps="epf_only"))
add("FAQ-Q7C", "EPFO FAQ golden", "EPF+EPS member capped 15,000 → 20,000 from 17-Sep", Employee(D(20000)))
for w in (10000, 15000, 20000, 25000, 35000):
    add(f"FAQ-Q13-{w // 1000}K", "EPFO FAQ golden", f"October onwards, PF wage ₹{w:,}",
        Employee(D(w), eps="member" if w <= 25000 else "none"), month="2026-10")

# ---------------------------------------------------------------- A joining dates
add("A01", "A Joining date", "DOJ before rule change (01-Sep-2026 joiner covered both parts)", Employee(D(30000), d(1), d(1)))
add("A02", "A Joining date", "DOJ exactly on rule change 17-Sep", Employee(D(30000), d(17), d(17)))
add("A03", "A Joining date", "DOJ after rule change 22-Sep", Employee(D(30000), d(22), d(22)))
add("A04", "A Joining date", "DOJ before payroll month (2019)", Employee(D(30000), date(2019, 4, 1), date(2019, 4, 1)))
add("A05", "A Joining date", "DOJ during month, before change (10-Sep)", Employee(D(30000), d(10), d(10)))
add("A06", "A Joining date", "DOJ on last day 30-Sep (1 day, NEW)", Employee(D(30000), d(30), d(30)))
add("A07", "A Joining date", "Future DOJ 05-Oct: nothing in September", Employee(D(30000), d(5, 10), d(5, 10)))
add("A08", "A Joining date", "DOJ 15-Sep (2 OLD days)", Employee(D(30000), d(15), d(15)))

# ---------------------------------------------------------------- B PF applicability
add("B01", "B PF applicable date", "PF applicable before change (01-Sep)", Employee(D(30000), d(1, 6), d(1)))
add("B02", "B PF applicable date", "PF applicable exactly on change 17-Sep", Employee(D(30000), d(1, 6), d(17)))
add("B03", "B PF applicable date", "PF applicable after change 24-Sep", Employee(D(30000), d(1, 6), d(24)))
add("B04", "B PF applicable date", "PF applicable date ≠ DOJ (DOJ 01-Sep, PF 10-Sep)", Employee(D(30000), d(1), d(10)))
add("B05", "B PF applicable date", "PF not applicable (exempt / no PF date)", Employee(D(30000), d(1, 6), None))
add("B06", "B PF applicable date", "PF applicable date before DOJ → data error", Employee(D(30000), d(10), d(1)))

# ---------------------------------------------------------------- C transition
add("C01", "C Transition", "Full OLD period (August, ceiling 15,000)", Employee(D(30000)), month="2026-08")
add("C02", "C Transition", "Full NEW period (October, ceiling 25,000)", Employee(D(30000)), month="2026-10")
add("C03", "C Transition", "OLD + NEW combined (full September)", Employee(D(30000)))
add("C04", "C Transition", "Exit exactly 16-Sep: OLD only", Employee(D(30000), exit=d(16)))
add("C05", "C Transition", "Joins exactly 17-Sep: NEW only", Employee(D(30000), d(17), d(17)))

# ---------------------------------------------------------------- mandatory boundary tests (section 25)
for day in (15, 16, 17, 18):
    add(f"BD-EXIT-{day}", "Boundary", f"Exit {day}-Sep", Employee(D(30000), exit=d(day)))
    add(f"BD-LOP-{day}", "Boundary", f"Single LOP on {day}-Sep (₹15,000 wage)", Employee(D(15000), lop=(d(day),)))
for day in (16, 17, 18):
    add(f"BD-DOJ-{day}", "Boundary", f"DOJ = {day}-Sep (PF from DOJ)", Employee(D(30000), d(day), d(day)))
    add(f"BD-PF-{day}", "Boundary", f"PF applicable date = {day}-Sep (DOJ 01-Jun)", Employee(D(30000), d(1, 6), d(day)))
    add(f"BD-REV-{day}", "Boundary", f"Revision on {day}-Sep ₹20,000 → ₹30,000", Employee(D(20000), revisions=((d(day), D(30000)),)))

# ---------------------------------------------------------------- D exit
add("D01", "D Exit", "Exit before change (10-Sep)", Employee(D(30000), exit=d(10)))
add("D02", "D Exit", "Exit on change 17-Sep (1 NEW day)", Employee(D(30000), exit=d(17)))
add("D03", "D Exit", "Exit after change (25-Sep)", Employee(D(30000), exit=d(25)))
add("D04", "D Exit", "Exit before period end (29-Sep)", Employee(D(30000), exit=d(29)))
add("D05", "D Exit", "Exit before DOJ → data error", Employee(D(30000), d(10), d(10), exit=d(5)))
add("D06", "D Exit", "Joins 10-Sep, exits 20-Sep (short stint across boundary)", Employee(D(30000), d(10), d(10), exit=d(20)))

# ---------------------------------------------------------------- E LOP
add("E01", "E LOP", "LOP only in OLD period (3, 4 Sep), ₹15,000", Employee(D(15000), lop=(d(3), d(4))))
add("E02", "E LOP", "LOP only in NEW period (22, 23 Sep), ₹15,000", Employee(D(15000), lop=(d(22), d(23))))
add("E03", "E LOP", "LOP crossing OLD/NEW (15–18 Sep), ₹15,000", Employee(D(15000), lop=(d(15), d(16), d(17), d(18))))
add("E04", "E LOP", "Multiple LOP periods (2, 9, 21, 28 Sep), ₹15,000", Employee(D(15000), lop=(d(2), d(9), d(21), d(28))))
add("E05", "E LOP", "LOP lowers earned wage below segment ceiling (₹16,000 wage, 5 LOP in NEW)", Employee(D(16000), lop=(d(21), d(22), d(23), d(24), d(25))))
add("E06", "E LOP", "Whole OLD segment on LOP (1–16 Sep)", Employee(D(30000), lop=tuple(d(x) for x in range(1, 17))))
add("E07", "E LOP", "LOP date before DOJ is ignored (joins 10-Sep, LOP 05-Sep)", Employee(D(30000), d(10), d(10), lop=(d(5),)), note="LOP outside employment ignored + warning")

# ---------------------------------------------------------------- F salary revision
add("F01", "F Revision", "Revision before change (10-Sep) ₹20,000 → ₹30,000", Employee(D(20000), revisions=((d(10), D(30000)),)))
add("F02", "F Revision", "Revision on change (17-Sep)", Employee(D(20000), revisions=((d(17), D(30000)),)))
add("F03", "F Revision", "Revision after change (20-Sep)", Employee(D(20000), revisions=((d(20), D(30000)),)))
add("F04", "F Revision", "Multiple revisions (10-Sep ₹18k, 24-Sep ₹28k)", Employee(D(14000), revisions=((d(10), D(18000)), (d(24), D(28000)))))
add("F06", "F Revision", "Revision ₹12,000 → ₹30,000 on 10-Sep: cap applies to the whole OLD period", Employee(D(12000), revisions=((d(10), D(30000)),)))
add("F05", "F Revision", "Revision + LOP + rule change together", Employee(D(20000), lop=(d(16), d(17), d(21)), revisions=((d(20), D(30000)),)))

# ---------------------------------------------------------------- G salary values
for label, w in (("below 15,000", 12000), ("exactly 15,000", 15000), ("between 15,000 and 25,000", 20000),
                 ("exactly 25,000", 25000), ("above 25,000", 40000)):
    tag = {12000: "01", 15000: "02", 20000: "03", 25000: "04", 40000: "05"}[w]
    add(f"G{tag}", "G Salary value", f"PF wage {label}: September split", Employee(D(w)))
    add(f"G{tag}-OCT", "G Salary value", f"PF wage {label}: October (NEW only)", Employee(D(w)), month="2026-10")

# ---------------------------------------------------------------- H policy
add("H01", "H PF wage policy", "statutory_ceiling, ₹40,000", Employee(D(40000)))
add("H02", "H PF wage policy", "actual_wages, ₹40,000 (contribution on full wage, EPS still capped)", Employee(D(40000), policy="actual_wages"))
add("H03", "H PF wage policy", "actual_wages, ₹40,000, EDLI capped variant (decision D3)", Employee(D(40000), policy="actual_wages"), edli_basis="capped")
add("H04", "H PF wage policy", "actual_wages, ₹12,000 (below ceiling: same as statutory)", Employee(D(12000), policy="actual_wages"))
add("H05", "H PF wage policy", "actual_wages, October ₹40,000", Employee(D(40000), policy="actual_wages"), month="2026-10")

# ---------------------------------------------------------------- I categories
add("I01", "I Category", "Existing PF+EPS member ₹30,000", Employee(D(30000)))
add("I02", "I Category", "Newly covered (excluded ₹22,000 earner) from 17-Sep", Employee(D(22000), date(2023, 5, 1), d(17)))
add("I03", "I Category", "Non-PF employee (above ceiling at joining, not opted)", Employee(D(40000), date(2023, 5, 1), None))
add("I04", "I Category", "Intern (stipend, PF not applicable)", Employee(D(10000), d(1, 7), None))
add("I05", "I Category", "EPF-only member ₹22,000 joins EPS on 17-Sep", Employee(D(22000), eps="epf_only"))
add("I06", "I Category", "EPF-only member ₹30,000 stays EPF-only (wage > 25,000)", Employee(D(30000), eps="epf_only"))
add("I07", "I Category", "Turns 58 on 10-Sep: EPS stops from birthday", Employee(D(30000), dob=date(1968, 9, 10)))
add("I08", "I Category", "Already 58+ (EPS nil whole month)", Employee(D(30000), dob=date(1960, 1, 1)))

