"""Entity extraction: employee (code or name), payroll period and work state."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PeriodError

_CODE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9})-(\d{1,6})\b")
_ISO = re.compile(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b")
_MONTH_FIRST = re.compile(r"\b(0?[1-9]|1[0-2])[-/](20\d{2})\b")
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4,
    "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip
_NAMED = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s*[-,']?\s*(20\d{2})\b",
    re.IGNORECASE,
)


@dataclass
class Entities:
    employee_code: str | None = None
    employee_source: str | None = None  # "code" | "name" | "memory"
    period: str | None = None
    period_source: str | None = None  # "text" | "memory"
    state: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "employee_code": self.employee_code, "employee_source": self.employee_source,
            "period": self.period, "period_source": self.period_source, "state": self.state,
            "notes": list(self.notes),
        }  # fmt: skip


def _is_period_token(match: re.Match[str]) -> bool:
    """'Sep-2026' looks like an employee code but is a payroll month."""
    prefix, number = match.group(1).lower(), match.group(2)
    return prefix in _MONTHS and re.fullmatch(r"20\d{2}", number) is not None


def extract_period(text: str) -> str | None:
    for pattern, order in ((_ISO, "ym"), (_MONTH_FIRST, "my"), (_NAMED, "named")):
        match = pattern.search(text)
        if not match:
            continue
        try:
            if order == "ym":
                return str(PayrollPeriod(int(match.group(1)), int(match.group(2))))
            if order == "my":
                return str(PayrollPeriod(int(match.group(2)), int(match.group(1))))
            return str(PayrollPeriod(int(match.group(2)), _MONTHS[match.group(1).lower()]))
        except PeriodError:
            continue
    return None


def extract_entities(
    text: str,
    directory: Iterable[tuple[str, str]] = (),
    known_states: Iterable[str] = (),
) -> Entities:
    """``directory`` = (employee_code, full_name) pairs used to resolve names."""
    entities = Entities()
    directory = list(directory)
    codes = {code for code, _ in directory}

    match = next((m for m in _CODE.finditer(text) if not _is_period_token(m)), None)
    if match:
        code = f"{match.group(1)}-{match.group(2)}".upper()
        if not codes or code in codes:
            entities.employee_code, entities.employee_source = code, "code"
        else:
            entities.notes.append(f"Employee code {code} not found")
    if entities.employee_code is None and directory:
        words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", text)}
        hits = {
            code
            for code, name in directory
            if any(part.lower() in words for part in name.split() if len(part) >= 3)
        }
        if len(hits) == 1:
            entities.employee_code, entities.employee_source = hits.pop(), "name"
        elif len(hits) > 1:
            entities.notes.append(f"Name matches several employees: {sorted(hits)}")

    entities.period = extract_period(text)
    if entities.period:
        entities.period_source = "text"

    states = set(known_states)
    for token in re.findall(r"\b[A-Z]{2}\b", text):
        if token in states:
            entities.state = token
            break
    return entities
