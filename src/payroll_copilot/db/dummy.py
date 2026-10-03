"""Deterministic fake employees for demos and load tests (same people on every run).

Mix of departments, work states and salary bands (some ESIC, some taxable); about 20%
have unapproved absences in September 2026 (LOP review), some have leaves, and a few
joined in September (partial month, no August payslip).
"""

import random
from datetime import date, timedelta
from typing import Any

DUMMY_PREFIX = "EMP"
DUMMY_FIRST_NUMBER = 201

_FIRST = {
    "M": ["Aarav", "Vivaan", "Aditya", "Arjun", "Sai", "Karan", "Rohit", "Vikram", "Amit",
          "Suresh", "Manish", "Nikhil", "Rajesh", "Sandeep", "Imran", "Farhan", "Deepak",
          "Gaurav", "Harsh", "Yash"],
    "F": ["Ananya", "Diya", "Isha", "Kavya", "Neha", "Pooja", "Riya", "Sneha", "Shreya",
          "Meera", "Nisha", "Fatima", "Sana", "Lakshmi", "Divya", "Swati", "Tanvi", "Aditi",
          "Pallavi", "Ritu"],
}  # fmt: skip
_LAST = ["Sharma", "Verma", "Gupta", "Singh", "Kumar", "Patel", "Shah", "Iyer", "Nair",
         "Reddy", "Rao", "Das", "Bose", "Mukherjee", "Khan", "Joshi", "Kulkarni", "Desai",
         "Menon", "Pillai", "Chopra", "Malhotra", "Agarwal", "Bansal", "Mehta"]  # fmt: skip
_DEPARTMENTS = [  # (department, designations, weight)
    ("Engineering", ["Software Engineer", "Senior Engineer", "Tech Lead", "QA Engineer"], 5),
    ("Sales", ["Sales Executive", "Account Manager", "Sales Manager"], 4),
    ("Operations", ["Associate", "Operations Executive", "Team Lead"], 3),
    ("Customer Support", ["Support Executive", "Senior Support Executive"], 3),
    ("Finance", ["Accountant", "Analyst", "Finance Manager"], 2),
    ("Marketing", ["Marketing Executive", "Content Writer", "Marketing Manager"], 2),
    ("Human Resources", ["HR Executive", "Recruiter"], 1),
]
_STATES = [("MH", 6), ("KA", 5), ("DL", 4), ("TN", 3), ("TG", 3), ("WB", 2), ("GJ", 2),
           ("UP", 2), ("HR", 2)]  # fmt: skip
_GROSS_BANDS = [((15000, 21000), 3), ((22000, 40000), 5), ((40000, 90000), 4),
                ((90000, 200000), 2)]  # fmt: skip
_PAN_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_SEP_WORKDAYS = [d for d in range(1, 31) if date(2026, 9, d).weekday() != 6 and d != 14]


def _pick(rng: random.Random, weighted: list[tuple]) -> Any:
    return rng.choices([w[0] for w in weighted], weights=[w[-1] for w in weighted])[0]


def dummy_employee_specs(
    count: int, first_number: int = DUMMY_FIRST_NUMBER, seed: int = 42
) -> list[dict[str, Any]]:
    """Employee dicts in the same shape as the seed file's ``employees`` entries."""
    rng = random.Random(seed + first_number)
    specs = []
    for i in range(count):
        number = first_number + i
        gender = rng.choice(["M", "F"])
        first, last = rng.choice(_FIRST[gender]), rng.choice(_LAST)
        department = _pick(rng, _DEPARTMENTS)
        designations = next(d[1] for d in _DEPARTMENTS if d[0] == department)
        low, high = _pick(rng, _GROSS_BANDS)
        gross = rng.randrange(low, high + 1, 500)
        if rng.random() < 0.04:  # joined in September: partial month
            doj = date(2026, 9, rng.randint(2, 21))
        else:
            doj = date(2018, 1, 1) + timedelta(days=rng.randint(0, 3100))

        sep_days = [d for d in _SEP_WORKDAYS if date(2026, 9, d) >= doj]
        rng.shuffle(sep_days)
        absences = []
        if rng.random() < 0.2 and sep_days:
            absences = sorted(date(2026, 9, d) for d in sep_days[: rng.randint(1, 3)])
        leaves = []
        if rng.random() < 0.3 and len(sep_days) > 4:
            day = date(2026, 9, sep_days[4])  # never one of the absence days
            leaves.append({"leave_type": rng.choice(["CL", "SL"]), "from_date": day,
                           "to_date": day, "status": "APPROVED", "reason": "Personal"})
        if rng.random() < 0.1:
            day = date(2026, 10, rng.randint(12, 23))
            if day.weekday() != 6:
                leaves.append({"leave_type": "EL", "from_date": day, "to_date": day,
                               "status": "PENDING", "reason": "Family trip"})

        pan = ("".join(rng.choices(_PAN_LETTERS, k=3)) + "P" + last[0].upper()
               + f"{rng.randint(1000, 9999)}" + rng.choice(_PAN_LETTERS))  # fmt: skip
        specs.append({
            "employee_code": f"{DUMMY_PREFIX}-{number}",
            "full_name": f"{first} {last}",
            "email": f"{first}.{last}.{number}@example.com".lower(),
            "gender": gender,
            "dob": date(rng.randint(1972, 2003), rng.randint(1, 12), rng.randint(1, 28)),
            "doj": doj,
            "work_state": _pick(rng, _STATES),
            "department": department,
            "designation": rng.choice(designations),
            "uan": f"1009{number:08d}",
            "pan": pan,
            "pf_join_date": doj,
            "esic_applicable": gross <= 21000,
            "tax_regime": "NEW" if rng.random() < 0.8 else "OLD",
            "salary": {"template": "LOW-2026" if gross < 25000 else "STD-2026",
                       "monthly_gross": gross,
                       "effective_from": doj if doj > date(2026, 4, 1) else date(2026, 4, 1)},
            "leaves": leaves,
            "absences": absences,
            "scenario": "generated",
        })  # fmt: skip
    return specs
