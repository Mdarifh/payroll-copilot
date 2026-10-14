"""Deterministic intent router: what the user wants, about whom, for which month.

Runs before the LLM. It never executes a write: WRITE intents only become a preview
(what-if simulation), which is applied later through the confirmation flow. Intents the
router does not handle (own payslip, leave, tax, small talk) go to the LLM tool loop.

Understands English and Hinglish ("Arif ka September PF kitna hai?",
"Arif ko 5 hours overtime do", "Arif ka LOP 4 days kar do").
"""

import calendar
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod, parse_month
from payroll_copilot.core.exceptions import PeriodError
from payroll_copilot.db.models import Employee

READ, WRITE, PENDING, NONE = "READ", "WRITE", "PENDING", "NONE"

READ_INTENTS = ("employee_lookup", "salary_lookup", "payroll_lookup", "attendance_lookup",
                "payslip_lookup", "payroll_explanation", "payroll_comparison",
                "compliance_question", "payroll_audit", "anomaly_detection",
                "root_cause_analysis", "leave_balance", "tax_summary", "my_leave_requests",
                "leave_approvals", "pending_actions")  # fmt: skip
WRITE_INTENTS = ("update_salary", "update_lop", "add_overtime", "add_miscellaneous", "add_bonus",
                 "update_attendance", "update_salary_structure", "generate_payslip",
                 "publish_payslip", "run_payroll")  # fmt: skip

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | {
    m.lower(): i for i, m in enumerate(calendar.month_abbr) if m} | {"sept": 9}
_MONTH_RE = re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True))
                       # Nov 2026, Nov, 2026, nov-2026, aug/2026, nov-26, nov'26
                       + r")\b(?:[\s.,/-]*(\d{4})\b|['’-](\d{2})\b)?",
                       re.IGNORECASE)  # fmt: skip
_YM_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})\b")
_CODE_RE = re.compile(r"\b([A-Za-z]{2,4}-\d{2,5})\b")
_NUM = r"(\d+(?:\.\d+)?)"
_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(k|lakh|lac|l)?|"
                        r"\b([\d,]{3,}(?:\.\d+)?)\s*(k|lakh|lac)?\b|\b(\d+(?:\.\d+)?)\s*(k|lakh|lac)\b",
                        re.IGNORECASE)  # fmt: skip
_DAYS_RE = re.compile(_NUM + r"\s*(?:days?|din)\b", re.IGNORECASE)
_HOURS_RE = re.compile(_NUM + r"\s*(?:hours?|hrs?|h|ghante?|ghanta)\b", re.IGNORECASE)
_SELF = {"my", "mine", "me", "mera", "meri", "mere", "mujhe", "i", "apna", "apni", "apne"}
_STOP = {"ka", "ki", "ke", "ko", "kya", "hai", "mein", "main", "me", "payroll", "salary",
         "september", "october", "november", "december", "january", "february", "march",
         "april", "may", "june", "july", "august", "the", "and", "for", "lop", "pf", "esic",
         "tds", "pt", "net", "gross", "pay", "show", "check", "explain", "why", "kyun",
         "kitna", "kitni", "change", "days", "hours", "overtime", "bonus", "add", "karo",
         "kar", "do", "set", "make", "employee", "attendance", "structure", "month",
         "miscellaneous", "earning", "deduction", "compare", "run", "find", "issues"}

_WRITE_VERB = re.compile(
    r"\b(kar ?do|kar ?de|kar ?dijiye|kardo|karde|set|make|change|update|mark|apply|lagao|"
    r"laga ?do|daal ?do|dal ?do|add|give|grant|increase|decrease|revise|badha ?do|"
    r"ghata ?do|de ?do|dedo|pay)\b|\bdo[.!]?\s*$|\bkaro[.!]?\s*$", re.IGNORECASE)
_STATUS_WORDS = [("HALF_DAY", r"half.?day|aadha din"), ("WEEK_OFF", r"week.?off"),
                 ("HOLIDAY", r"holiday"), ("ABSENT", r"absent|gair.?hazir|ghair.?hazir"),
                 ("PRESENT", r"present|hazir|haazir")]  # fmt: skip
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]*(" + "|".join(
    sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?,?\s*(\d{4})?", re.IGNORECASE)


_LEAVE_TYPES = [("SL", r"\b(sick|sl|bimar\w*|fever|unwell|ill)\b"),
                ("EL", r"\b(earned|el|privilege|pl)\b"),
                ("LOP", r"\b(lop|unpaid|without pay|leave without)\b"),
                ("CL", r"\b(casual|cl)\b")]  # fmt: skip
_RANGE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|to|se|till|until)\s*(\d{1,2})"
                       r"(?:st|nd|rd|th)?[\s-]*(" + "|".join(sorted(_MONTHS, key=len, reverse=True))
                       + r")\b", re.IGNORECASE)  # fmt: skip
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_RELATIVE = [(r"\bday after tomorrow\b|\bparso\b|\bparson\b", 2),
             (r"\btomorrow\b|\bkal\b", 1), (r"\btoday\b|\baaj\b", 0)]  # fmt: skip


def _leave_date(day: int, month: int, year: int | None, today: date) -> date | None:
    try:
        found = date(year or today.year, month, day)
    except ValueError:
        return None
    if year is None and found < today - timedelta(days=60):
        found = found.replace(year=found.year + 1)  # "5 Jan" in October = next January
    return found


def extract_leave(text: str, today: date, days: Decimal | None) -> dict[str, Any]:
    """'sick leave 20 to 22 Oct', 'kal CL chahiye', 'EL from 3 Nov for 2 days because ...'."""
    low = text.lower()
    out: dict[str, Any] = {"leave_type": None, "from": None, "to": None, "reason": ""}
    out["leave_type"] = next((t for t, pattern in _LEAVE_TYPES if re.search(pattern, low)), None)
    dates: list[date] = []
    if (m := _RANGE_RE.search(text)) is not None:
        month = _MONTHS[m.group(3).lower()]
        dates = [d for d in (_leave_date(int(m.group(1)), month, None, today),
                             _leave_date(int(m.group(2)), month, None, today)) if d]
    if not dates:
        dates = [date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                 for m in _ISO_RE.finditer(text)]  # fmt: skip
    if not dates:
        for m in _DAY_MONTH_RE.finditer(text):
            found = _leave_date(int(m.group(1)), _MONTHS[m.group(2).lower()],
                                int(m.group(3)) if m.group(3) else None, today)  # fmt: skip
            if found:
                dates.append(found)
    if not dates:
        dates = [today + timedelta(days=n)
                 for pattern, n in _RELATIVE if re.search(pattern, low)][:1]  # fmt: skip
    if dates:
        start = dates[0]
        end = dates[1] if len(dates) > 1 else start
        if len(dates) == 1 and days and days > 1:  # "for 3 days": count working days
            left, end = int(days) - 1, start
            while left > 0:
                end += timedelta(days=1)
                if end.weekday() != 6:
                    left -= 1
        out["from"], out["to"] = start.isoformat(), max(start, end).isoformat()
    if (m := re.search(r"\b(?:because|reason\s*:?|kyunki|kyuki|due to)\s+(.{3,200})$", text,
                       re.IGNORECASE)) is not None:  # fmt: skip
        out["reason"] = m.group(1).strip().rstrip(".")
    return out


_SECTION_RE = re.compile(r"\b(80\s*ccd\s*\(?1b\)?|nps|80\s*c|80\s*d|80\s*tta)\b", re.IGNORECASE)


def extract_declarations(text: str) -> dict[str, Decimal]:
    """'80C 1.5 lakh, 80D 25000' -> {"80C": 150000, "80D": 25000}."""
    found: dict[str, Decimal] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        name = re.sub(r"\s+", "", m.group(1).upper())
        name = "80CCD(1B)" if name.startswith("80CCD") or name == "NPS" else name
        tail = text[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        amount = extract_amount(tail[:40]) or (
            _dec(n.group(1)) if (n := re.match(r"\D{0,15}?(\d+(?:\.\d+)?)\b", tail)) else None)
        if amount:
            found[name] = amount
    return found


def extract_attendance(text: str, today: date) -> list[dict[str, str]]:
    """'15 Sep absent', '3rd october ko present' -> [{date, status}] (one status for all)."""
    low = text.lower()
    status = next((name for name, pattern in _STATUS_WORDS if re.search(pattern, low)), None)
    if status is None:
        return []
    out = []
    for m in _DAY_MONTH_RE.finditer(text):
        month = _MONTHS[m.group(2).lower()]
        year = int(m.group(3)) if m.group(3) else (
            today.year if month <= today.month else today.year - 1)
        with suppress(ValueError):
            out.append({"date": date(year, month, int(m.group(1))).isoformat(), "status": status})
    return out


_COMPONENT = [("PF", r"\b(pf|provident fund|epf)\b"), ("ESIC", r"\besic?\b"),
              ("PT", r"\b(pt|professional tax)\b"), ("TDS", r"\b(tds|income tax|tax)\b"),
              ("LWF", r"\blwf\b"), ("LOP", r"\b(lop|loss of pay)\b"),
              ("NET", r"\b(net pay|net salary|in.?hand|net)\b"),
              ("GROSS", r"\bgross\b")]  # fmt: skip


@dataclass
class Route:
    intent: str = "none"
    kind: str = NONE
    employee: str | None = None  # employee code, or None = the user
    employee_error: str | None = None
    months: list[PayrollPeriod] = field(default_factory=list)
    component: str | None = None
    days: Decimal | None = None
    hours: Decimal | None = None
    amount: Decimal | None = None
    flags: set[str] = field(default_factory=set)
    text: str = ""
    attendance: list[dict[str, str]] = field(default_factory=list)  # [{date, status}]
    extra: dict[str, Any] = field(default_factory=dict)  # leave request / tax declarations

    @property
    def month(self) -> PayrollPeriod | None:
        return self.months[0] if self.months else None


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def extract_months(text: str, today: date) -> list[PayrollPeriod]:
    """Months in the order written. A month without a year is the latest such month
    not after today ("September" in Oct 2026 = Sep 2026; "December" = Dec 2025)."""
    found: list[tuple[int, PayrollPeriod]] = []
    for m in _YM_RE.finditer(text):
        with suppress(PeriodError):
            found.append((m.start(), parse_month(m.group(0))))
    for m in _MONTH_RE.finditer(text):
        name = m.group(1).lower()
        year = m.group(2) or (m.group(3) and f"20{m.group(3)}")
        if name == "may" and not year and not re.search(r"\bmay\s+(?:mein|me|ka|ki)\b|\bmay\b$",
                                                        text, re.IGNORECASE):  # fmt: skip
            continue  # "may" is usually the English verb
        month = _MONTHS[name]
        y = int(year) if year else (today.year if month <= today.month else today.year - 1)
        found.append((m.start(), PayrollPeriod(y, month)))
    low = text.lower()
    current = PayrollPeriod.from_date(today)
    for phrase, period in ((r"\b(last|previous|pichle|pichhle) (month|mahine)\b",
                            current.previous()),
                           (r"\b(this|current|is) (month|mahine)\b", current)):  # fmt: skip
        if (m := re.search(phrase, low)) is not None:
            found.append((m.start(), period))
    seen: list[PayrollPeriod] = []
    for _, p in sorted(found, key=lambda x: x[0]):
        if p not in seen:
            seen.append(p)
    return seen


def extract_amount(text: str) -> Decimal | None:
    for m in _AMOUNT_RE.finditer(text):
        raw, unit = (m.group(1), m.group(2)) if m.group(1) else (
            (m.group(3), m.group(4)) if m.group(3) else (m.group(5), m.group(6)))  # fmt: skip
        value = _dec(raw)
        if value is None:
            continue
        unit = (unit or "").lower()
        if unit == "k":
            value *= 1000
        elif unit in ("lakh", "lac", "l"):
            value *= 100000
        if _YM_RE.fullmatch(raw) or (value >= 1900 and value <= 2100 and not unit
                                     and "₹" not in m.group(0) and "rs" not in m.group(0).lower()):
            continue  # a year, not money
        return value
    return None


def extract_employee(session: Session, user: Employee, text: str) -> tuple[str | None, str | None]:
    """(employee code or None for the user, error). Names are matched against employees."""
    if m := _CODE_RE.search(text):
        code = m.group(1).upper()
        if session.scalar(select(Employee.id).where(Employee.employee_code == code)):
            return code, None
    words = [w for w in re.findall(r"[a-z]+", text.lower())]
    if any(w in _SELF for w in words):
        return None, None
    candidates = [w for w in words if len(w) >= 3 and w not in _STOP]
    if not candidates:
        return None, None
    matches: dict[int, Employee] = {}
    for e in session.scalars(select(Employee)):
        parts = set(e.full_name.lower().split())
        if parts & set(candidates):
            matches[e.id] = e
    if not matches:
        return None, None
    if len(matches) > 1:
        # prefer full-name matches ("Rahul Verma") over first names shared by several people
        both = [e for e in matches.values() if len(set(e.full_name.lower().split())
                                                    & set(candidates)) > 1]
        if len(both) == 1:
            return both[0].employee_code, None
        names = ", ".join(f"{e.full_name} ({e.employee_code})"
                          for e in sorted(matches.values(), key=lambda x: x.full_name)[:6])
        return None, f"More than one employee matches: {names}. Which one did you mean?"
    only = next(iter(matches.values()))
    return (None if only.id == user.id else only.employee_code), None


def _component(text: str) -> str | None:
    low = text.lower()
    for name, pattern in _COMPONENT:
        if re.search(pattern, low):
            return name
    return None


def route(session: Session, user: Employee, message: str, today: date) -> Route:
    text = message.strip()
    low = text.lower()
    r = Route(months=extract_months(text, today), component=_component(text), text=text)
    r.employee, r.employee_error = extract_employee(session, user, text)
    # numbers: ignore employee codes (EMP-101) and dates (2026-09) when reading amounts
    numbers = _YM_RE.sub(" ", _CODE_RE.sub(" ", text))
    if (m := _DAYS_RE.search(numbers)) is not None:
        r.days = _dec(m.group(1))
    if (m := _HOURS_RE.search(numbers)) is not None:
        r.hours = _dec(m.group(1))
    r.amount = extract_amount(numbers)
    write = bool(_WRITE_VERB.search(low))
    asks = bool(re.search(r"\b(kitna|kitni|kitne|how much|what is|what's|kya hai|show|batao|"
                          r"bata|explain|samjhao|check|dikhao|kaise|how)\b|\?", low))

    def set_(intent: str, kind: str) -> Route:
        r.intent, r.kind = intent, kind
        return r

    # ---- leave: requests to decide / my requests / balance ---------------------------------
    leave_word = re.search(r"\b(leaves?|chh?utt?i|chhuti|cl|sl|el)\b", low)
    process_question = re.search(r"\b(how|kaise|process|policy|rules?|steps|kya hota)\b", low)
    # "who approves leave?" is a policy question, not a request to see the approvals
    who_question = re.search(r"\b(who|kaun|why|kyun|what is|kya hai)\b", low)
    mine = re.search(r"\b(my|mine|mera|meri|mere|apni|apna)\b", low)
    if leave_word and not process_question and not r.employee_error:
        if re.search(r"\b(cancel\w*|withdraw\w*)\b", low) or (mine and re.search(
                r"\b(status|pending|approved?|rejected|requests)\b", low)):  # fmt: skip
            if re.search(r"\b(cancel|withdraw)", low):
                r.flags.add("cancel")
            return set_("my_leave_requests", READ)
        if not who_question and re.search(
                r"\b(pending|approv\w*|reject\w*|requests|to.?decide|manzoor)\b", low):
            return set_("leave_approvals", READ)
        if not r.employee and re.search(r"\bbalance\b|\bkitn\w* (leaves?|chh?utt?i)|"
                                        r"\b(leaves?|chh?utt?i\w*) (left|remaining|baki|bachi)",
                                        low):  # fmt: skip
            return set_("leave_balance", READ)
    # ---- payroll changes waiting for approval (maker-checker) --------------------------------
    if not leave_word and re.search(
            r"\b(pending|waiting)\b.{0,30}\b(approvals?|actions?|changes?|requests?)\b|"
            r"\bapprovals?\b.{0,20}\b(pending|dikhao|show|list|batao)\b|\baction cent", low):
        return set_("pending_actions", READ)

    # ---- leave application and tax help ---------------------------------------------------
    if leave_word and not re.search(
            r"\b(how|kaise|process|policy|rules?|steps|balance|kitn\w*|mark\w*|absent|"
            r"present|attendance)\b|\?", low):  # fmt: skip
        found = extract_leave(text, today, r.days)
        if found["from"] or re.search(
                r"\b(apply|lagao|laga ?do|lagana|lagani|chahiye|chaiye|chahie|request|take|"
                r"lena|leni|book|want|need|le ?raha|le ?rahi)\b", low):  # fmt: skip
            r.extra = found
            return set_("apply_leave", WRITE)
    if re.search(r"\b(80\s*c\w*|80\s*d|80\s*tta|nps|tax.?sav\w*|save tax|tax bacha\w*|"
                 r"(old|new|which|kaun\w*|konsa|better|best|compare)\b.{0,25}\bregime|"
                 r"regime\b.{0,25}\b(better|best|choose|compare|vs|should|kaun\w*|konsa|"
                 r"switch|change))", low):  # fmt: skip
        r.extra = {"declared": extract_declarations(text)}
        return set_("tax_help", READ)

    # ---- write intents (preview only) -------------------------------------------------
    run_words = r"run|process|chalao|chala ?do|start"
    if re.search(rf"\b({run_words})\b.*\bpayroll\b|\bpayroll\b.*\b({run_words}|review)\b",
                 low) and not (
            re.search(r"\baudit\b|\b(how|kaise|steps|procedure|sop|kya hai|what is|explain|"
                      r"samjhao|policy)\b", low)) and (  # fmt: skip
            # "process" alone is the noun ("payroll process"), unless asked to do it
            re.search(r"\b(run|chalao|chala ?do|start|review)\b", low) or r.month
            or re.search(r"\b(karo|kar ?do|kijiye|karna|please|now|all|sab)\b", low)):
        # "run AI payroll audit" is an audit; "how to run payroll" is a process question
        # named employees (codes, even unknown ones, or a name) run only them, never everyone
        codes = [c.upper() for c in _CODE_RE.findall(text)]
        named = list(dict.fromkeys(([r.employee] if r.employee else []) + codes))
        if named:
            r.extra = {"employees": named}
        return set_("run_payroll", WRITE)
    if re.search(r"\b(publish|release|send|bhej)\b.*\bpayslips?\b", low):
        return set_("publish_payslip", PENDING)
    if re.search(r"\b(generate|bana)\b.*\bpayslips?\b", low):
        return set_("generate_payslip", PENDING)
    r.attendance = extract_attendance(text, today)
    if r.attendance and (write or re.search(r"\bmark\w*\b", low)):
        r.months = [PayrollPeriod.parse(r.attendance[0]["date"][:7])]
        # an absence means loss of pay unless HR says otherwise ("without LOP", "LOP mat")
        if not re.search(r"\b(without|no|bina)\s+lop\b|\blop\s+(mat|nahi|na)\b|"
                         r"\bno\s+loss of pay\b", low):  # fmt: skip
            r.flags.add("with_lop")
        return set_("update_attendance", WRITE)
    if write and not asks or (write and (r.hours or r.days or r.amount)):
        if re.search(r"\b(overtime|ot)\b", low) and r.hours:
            return set_("add_overtime", WRITE)
        if re.search(r"\blop\b|loss of pay", low) and r.days is not None:
            return set_("update_lop", WRITE)
        if re.search(r"\bbonus\b", low) and r.amount:
            return set_("add_bonus", WRITE)
        if re.search(r"\b(misc|miscellaneous|other earning|reimburse|deduction|deduct|recover|"
                     r"kaat|kaato)\b", low) and r.amount:  # fmt: skip
            if re.search(r"\b(deduction|deduct|recover|kaat|kaato)\b", low):
                r.flags.add("deduction")
            if re.search(r"non.?taxable|tax.?free|exempt", low):
                r.flags.add("non_taxable")
            return set_("add_miscellaneous", WRITE)
        if re.search(r"\bstructure\b", low):
            return set_("update_salary_structure", PENDING)
        if re.search(r"\b(salary|ctc|gross|increment|hike|revise|revision)\b", low) and r.amount:
            if re.search(r"\b(ctc|annual|per annum|lpa|yearly|lakh|lac)\b", low):
                r.flags.add("annual")
            # a revision is about a coming month: "from November" in Oct 2026 = Nov 2026
            current = PayrollPeriod.from_date(today)
            r.months = [PayrollPeriod(m.year + 1, m.month)
                        if m < current.previous() and str(m.year) not in text else m
                        for m in r.months]
            return set_("update_salary", WRITE)
        if re.search(r"\battendance\b", low):
            return set_("update_attendance", PENDING)

    # ---- audit / anomalies (Phase 8) ----------------------------------------------------
    if re.search(r"\b(audit|anomal\w*|mismatch\w*|issues?|problems?|galat|wrong|negative arrears?|"
                 r"root cause)\b", low):  # fmt: skip
        if re.search(r"\barrears?\b", low):
            r.flags.add("arrears")
        return set_("payroll_audit", READ)

    # ---- read intents --------------------------------------------------------------------
    if re.search(r"\b(compare|comparison|vs\.?|versus|difference|farak|fark)\b", low) or (
            re.search(r"\b(why|kyun|kyon|kaise)\b", low)
            and re.search(r"\b(change\w*|badl\w*|kam|zyada|less|more|increase\w*|decrease\w*|"
                          r"different|differ)\b", low)):  # fmt: skip
        return set_("payroll_comparison", READ)
    if r.component in ("PF", "ESIC", "PT", "TDS", "LWF") and (
            re.search(r"\b(rule|rules|rate|rates|ceiling|slab|slabs|limit|policy|niyam)\b", low)
            or (not r.employee and not r.months
                and re.search(r"\b(what is|kya hai|explain|samjhao|meaning|matlab)\b", low))):
        return set_("compliance_question", READ)
    # a component question about someone, or "how / how much" about one's own pay
    how =re.search(r"\b(explain|samjhao|how|kaise|calculate|working|why|kyun|kitna|kitni)\b",
                    low)  # fmt: skip
    if r.component and (r.employee or how) and (r.component != "NET" or r.employee or asks):
        return set_("payroll_explanation", READ)
    if re.search(r"\b(salary structure|structure|ctc|basic|hra|allowance)\b", low) and (
            r.employee or asks):
        return set_("salary_lookup", READ)
    if re.search(r"\b(attendance|absent|present|leave days|haaziri|hazri)\b", low) and r.employee:
        return set_("attendance_lookup", READ)
    if re.search(r"\bpayslip|salary slip|pay slip\b", low) and (r.employee or not re.search(
            r"\b(how|kaise|why|kyun|when|kab|policy)\b", low)):  # fmt: skip
        return set_("payslip_lookup", READ)  # no employee = the user's own
    if not r.employee and re.search(
            r"\btax summary\b|\b(my|mera|meri|mere)\s+(income\s+)?(tax|tds)\b|"
            r"\b(tax|tds)\b.{0,15}\b(kitna|kitni|how much)\b|"
            r"\b(kitna|kitni|how much)\b.{0,15}\b(tax|tds)\b", low):  # fmt: skip
        return set_("tax_summary", READ)
    if r.employee and re.search(r"\b(who|kaun|details?|profile|info|about|employee)\b", low):
        return set_("employee_lookup", READ)
    if r.employee and re.search(r"\b(salary|pay|tankhwah)\b", low):
        return set_("payroll_lookup", READ)
    # policy / procedure questions -> knowledge base (never personal data like balances).
    # Clear policy words always qualify; a bare topic ("leave", "overtime") only with a
    # question word and without "my / mera" (that is the user's own data -> LLM tools).
    personal = re.search(r"\b(my|mine|mera|meri|mere|mujhe|balance|payslip|tax summary|"
                         r"form 16|ignore)\b", low)  # fmt: skip
    policy = re.search(r"\b(polic\w*|niyam|sop|procedure|maker.?checker|what happens|"
                       r"kya hota|kaise kare|how do i|how to)\b", low)  # fmt: skip
    topic = re.search(r"\b(rules?|allowed|eligib\w*|entitle\w*|leaves?|chh?utti\w*|gratuity|"
                      r"notice period|f&f|fnf|full and final|settlement|reimburs\w*|process|steps|"
                      r"approval|overtime|half.?day|arrears?|proration)\b", low)  # fmt: skip
    question = re.search(r"\b(kitn\w*|kya|kab|kaun|kaise|how|what|when|who|which|can|is|are|"
                         r"does|do|explain|batao|samjhao|rule|rules)\b", low)  # fmt: skip
    if not r.employee and not personal and (policy or (topic and question)):
        return set_("knowledge_question", READ)
    return r


# ---------------------------------------------------------------------- follow-ups

_FOLLOW = re.compile(r"\b(aur|bhi|also|what about|how about|uska|uski|uske|iska|iski|iske|"
                     r"unka|unki|same|wahi|usi|his|her|their)\b|^\s*(and|or)\b", re.IGNORECASE)
_SELF_RE = re.compile(r"\b(" + "|".join(sorted(_SELF - {"i"})) + r")\b", re.IGNORECASE)
FOLLOW_INTENTS = {"payroll_explanation", "payroll_comparison", "salary_lookup",
                  "attendance_lookup", "payslip_lookup", "employee_lookup", "payroll_lookup",
                  "compliance_question"}  # fmt: skip


def context_of(r: Route) -> dict[str, Any]:
    """What a later follow-up may reuse (read questions only - never a change)."""
    if r.kind != READ or r.intent not in FOLLOW_INTENTS:
        return {}
    return {"intent": r.intent, "employee": r.employee, "component": r.component,
            "months": [str(m) for m in r.months]}


def apply_followup(r: Route, previous: dict[str, Any]) -> Route:
    """"aur uska PF?", "October ka bhi", "what about EMP-102": fill what the message leaves
    out from the previous read question. A change (WRITE) is never taken from context."""
    if previous and r.intent == "employee_lookup" and re.search(r"\b(what|how) about\b",
                                                                r.text, re.IGNORECASE):
        r.intent, r.kind = "none", NONE  # "what about X" continues the last question
    if not previous or r.kind == WRITE or r.intent not in ("none", previous.get("intent")):
        return r
    words = len(r.text.split())
    if not (_FOLLOW.search(r.text) or (r.intent == "none" and words <= 6
                                       and (r.months or r.component or r.employee))):
        return r
    if r.employee is None and not _SELF_RE.search(r.text) and previous.get("employee"):
        r.employee = previous["employee"]
    if not r.months and previous.get("months"):
        r.months = [PayrollPeriod.parse(m) for m in previous["months"]]
    if r.intent == "none":
        if r.component and previous["intent"] != "payroll_explanation":
            r.intent = "payroll_explanation"
        else:
            r.intent = previous["intent"]
            r.component = r.component or previous.get("component")
        r.kind = READ
        r.flags.add("followup")
    return r
