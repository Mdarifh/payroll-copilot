"""Run a routed intent: deterministic tools in, facts + cards + a plain English answer out.

WRITE intents never change data here: they build a what-if preview (simulation, rolled
back) shown as an ``action_preview`` card. Applying it needs the confirmation flow.
"""

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from payroll_copilot.auth.permissions import Perm, change_perm, has, is_staff
from payroll_copilot.chat.router import PENDING, READ, WRITE, Route
from payroll_copilot.chat.tools import ToolContext
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    NotFoundError,
    PayrollCopilotError,
    PermissionDeniedError,
)
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Attendance, Employee, PayrollRun, SalaryStructure
from payroll_copilot.services import employee_directory as directory
from payroll_copilot.services import payroll_insights as insights

ZERO = Decimal(0)
PENDING_TEXT = {
    "publish_payslip": "Publishing payslips is not available yet.",
    "generate_payslip": "Payslips are generated when payroll runs - use Payroll Runs.",
    "update_salary_structure": "Changing an employee's salary structure template arrives in "
                               "Phase 6 (salary actions).",
    "update_attendance": "Tell me the day and the status, e.g. 'EMP-101 ka 15 Sep absent mark "
                         "karo', or edit the calendar on the Employees > Attendance tab.",
}


@dataclass
class Routed:
    text: str  # deterministic English answer (always correct)
    facts: dict[str, Any] = field(default_factory=dict)  # what an LLM may reword
    cards: list[dict[str, Any]] = field(default_factory=list)
    tool: str = ""


def _latest_month(ctx: ToolContext, employee: Employee) -> PayrollPeriod:
    """Latest month with a payslip, else with attendance, else the current month."""
    period = ctx.session.scalar(select(PayrollRun.period).where(
        PayrollRun.employee_id == employee.id).order_by(PayrollRun.period.desc()))
    if period:
        return PayrollPeriod.parse(period)
    day = ctx.session.scalar(select(Attendance.day).where(Attendance.employee_id == employee.id)
                             .order_by(Attendance.day.desc()))  # fmt: skip
    return PayrollPeriod.from_date(day or ctx.today)


def _latest_attendance_month(ctx: ToolContext, employee: Employee) -> PayrollPeriod:
    day = ctx.session.scalar(select(Attendance.day).where(
        Attendance.employee_id == employee.id, Attendance.day <= ctx.today)
        .order_by(Attendance.day.desc()))  # fmt: skip
    return PayrollPeriod.from_date(day or ctx.today)


def _who(r: Route) -> str | None:
    return r.employee


def _inr(value: Any) -> str:
    return format_inr(Decimal(str(value)))


# ---------------------------------------------------------------------- reads


def _explain(ctx: ToolContext, r: Route) -> Routed:
    employee = insights.resolve_employee(ctx.session, ctx.user, _who(r))
    month = r.month or _latest_month(ctx, employee)
    x = insights.explain_component(ctx.session, ctx.user, employee.employee_code, month,
                                   r.component or "NET")  # fmt: skip
    name, comp = x["employee_name"], x["component"]
    if comp == "LOP":
        text = (f"{name} has {x['amount']} LOP day(s) in {x['month_label']}: paid days "
                f"{x['paid_days']} of {x['basis_days']}.")
    elif x.get("applicable") is False:
        text = f"No {comp} for {name} in {x['month_label']}: {x['reason']}."
    else:
        text = f"{name}'s {comp} for {x['month_label']} is {_inr(x['amount'])}."
        if comp == "PF" and x.get("segments"):
            parts = [f"{s['start'][8:]}–{s['end'][8:]} on PF wage {_inr(s['pf_wage'])} "
                     f"({s['rule_version']}, ceiling {_inr(s['pf_ceiling'])})"
                     for s in x["segments"]]
            text += " PF is split by date: " + "; ".join(parts) + "."
        elif x.get("formula"):
            text += f" Working: {x['formula']}."
    if x["difference_note"]:
        text += " " + x["difference_note"]
    card = {"type": "explain_card", **x}
    return Routed(text, x, [card], tool="explain_component")


def _compare(ctx: ToolContext, r: Route) -> Routed:
    employee = insights.resolve_employee(ctx.session, ctx.user, _who(r))
    if len(r.months) >= 2:
        a, b = r.months[0], r.months[1]
    else:
        b = r.month or _latest_month(ctx, employee)
        a = b.previous()
    try:
        c = insights.compare_months(ctx.session, ctx.user, employee.employee_code, a, b)
    except NotFoundError as exc:  # e.g. no attendance yet: compare the salary structures
        st = insights.compare_structures(ctx.session, ctx.user, employee.employee_code, a, b)
        change = Decimal(st["structure_change"])
        text = (f"Payroll for one of the months can't be calculated yet ({exc}). Comparing "
                f"{st['employee_name']}'s salary structure instead: monthly gross "
                + ("is unchanged" if change == 0 else
                   f"{'up' if change > 0 else 'down'} {_inr(abs(change))}")
                + f" from {st['first_label']} to {st['second_label']}"
                + ("; " + "; ".join(x["text"] for x in st["causes"]) if st["causes"] else
                   "; no salary revision in between") + ".")  # fmt: skip
        return Routed(text, st, [{"type": "compare_card", **st}], tool="compare_structures")
    net = Decimal(c["net_change"])
    trend = ("did not change" if net == 0 else
             f"went {'up' if net > 0 else 'down'} by {_inr(abs(net))}")  # fmt: skip
    text = f"{c['employee_name']}'s net pay {trend} from {c['first_label']} to {c['second_label']}."
    if c["causes"]:
        text += " Reasons found in the data: " + "; ".join(x["text"] for x in c["causes"]) + "."
    elif c["unexplained"]:
        text += " The data shows no salary, attendance or rule change that explains it."
    return Routed(text, c, [{"type": "compare_card", **c}], tool="compare_months")


def _rules(ctx: ToolContext, r: Route) -> Routed:
    month = r.month or PayrollPeriod.from_date(ctx.today)
    rules = insights.statutory_rules(ctx.session, ctx.user, r.component or "PF", month, _who(r))
    if rules["topic"] == "PF":
        parts = [f"{v['version']} ({v['from']} to {v['to']}): employee "
                 f"{v['employee_rate_percent']}%,"
                 f" employer {v['employer_rate_percent']}% (EPS {v['eps_rate_percent']}%), "
                 f"PF wage ceiling {_inr(v['pf_wage_ceiling'])}" for v in rules["versions"]]
        text = f"PF rules for {rules['month_label']}: " + "; ".join(parts) + "."
    elif rules["topic"] == "ESIC":
        text = (f"ESIC: employee {rules['employee_rate_percent']}%, employer "
                f"{rules['employer_rate_percent']}% on ESIC wages up to "
                f"{_inr(rules['wage_threshold'])} a month.")
    elif rules["topic"] == "PT":
        text = (f"Professional Tax for {rules['state']}: " + ("; ".join(
            f"above {_inr(s['above'])} up to "
            f"{s['up_to'] if s['up_to'] == 'no limit' else _inr(s['up_to'])}"
            f": {_inr(s['amount'])}" + (f" ({s['gender']})" if s["gender"] != "any" else "")
            for s in rules["slabs"]) if rules["levied"] else "not levied in this state") + ".")
    elif rules["topic"] == "TDS":
        text = (f"TDS uses the {rules['regime']} regime slabs for FY {rules['financial_year']} "
                f"with a standard deduction of {_inr(rules['standard_deduction'])}.")
    else:
        text = (f"LWF for {rules['state']}: employee {rules['employee_amount']}, "
                f"employer {rules['employer_amount']}.")
    if rules.get("status") == "UNVERIFIED":
        text += " Note: this rule is marked UNVERIFIED in the system."
    from payroll_copilot.services.knowledge import search

    cited = search(ctx.session, ctx.user, f"{rules['topic']} rate ceiling slab rule",
                   policy_type=rules["topic"].lower(), on=month.end, limit=1)  # fmt: skip
    if cited:
        rules["sources"] = [{"title": c["title"], "section": c["section"], "source": c["source"],
                             "status": c["status"]} for c in cited]
        text += f" Source: {cited[0]['title']} › {cited[0]['section'].split(' › ')[-1]}."
    return Routed(text, rules, [{"type": "rules_card", **rules}], tool="statutory_rules")


def _salary(ctx: ToolContext, r: Route) -> Routed:
    employee = insights.resolve_employee(ctx.session, ctx.user, _who(r), amounts=False)
    d = directory.salary_details(ctx.session, ctx.user, employee.employee_code, r.month)
    if d["restricted"]:
        name = d["profile"]["full_name"]
        return Routed(f"{name} is on {d['structure']['name']}. {d['message']}.")
    rows = [*d["earnings"]["rows"], *[x for x in d["allowances"]["rows"] if x["in_structure"]]]
    text = (f"{d['profile']['full_name']} ({d['structure']['code'] or 'custom'}) for "
            f"{d['month_label']}: monthly gross {_inr(d['monthly_gross'])}, annual gross "
            f"{_inr(d['annual_gross'])}. " + ", ".join(
                f"{x['head']} {_inr(x['monthly_amount'])}" for x in rows
                if x["in_structure"]) + ".")
    card = {"type": "info_card", "title": f"Salary structure · {d['profile']['full_name']}",
            "subtitle": f"{d['structure']['name']} · {d['month_label']}",
            "fields": [{"label": x["head"], "value": _inr(x["monthly_amount"]),
                        "hint": x["basis"]} for x in rows if x["in_structure"]]
            + [{"label": "Monthly gross", "value": _inr(d["monthly_gross"]), "strong": True},
               {"label": "Annual gross", "value": _inr(d["annual_gross"])}],
            "link": {"page": "employees", "label": "Open in Employees"}}  # fmt: skip
    return Routed(text, {k: d[k] for k in ("month", "monthly_gross", "annual_gross")} | {
        "components": {x["head"]: x["monthly_amount"] for x in rows}}, [card],
        tool="salary_details")  # fmt: skip


def _attendance(ctx: ToolContext, r: Route) -> Routed:
    employee = insights.resolve_employee(ctx.session, ctx.user, _who(r), amounts=False)
    a = directory.attendance_details(ctx.session, ctx.user, employee.employee_code, r.month)
    if not a["available"]:
        return Routed(a["message"] + ".")
    sm = a["summary"]
    text = (f"{a['full_name']} in {a['month_label']}: {sm['present_days']} present of "
            f"{sm['working_days']} working days, {sm['absent_days']} absent "
            f"({sm['unapproved_absences']} without approved leave), LOP {sm['lop_days']}, "
            f"paid days {sm['paid_days']} of {sm['basis_days']}.")
    fields = [{"label": k, "value": v} for k, v in (
        ("Working days", sm["working_days"]), ("Present", sm["present_days"]),
        ("Paid leave", sm["paid_leave_days"]), ("Absent", sm["absent_days"]),
        ("Without approved leave", sm["unapproved_absences"]), ("LOP", sm["lop_days"]),
        ("Paid days", f"{sm['paid_days']} / {sm['basis_days']}"))]
    absences = [d for d in a["days"] if d["status"] in ("ABSENT", "HALF_DAY")]
    card = {"type": "info_card", "title": f"Attendance · {a['full_name']}",
            "subtitle": a["month_label"], "fields": fields,
            "notes": [f"{d['date']}: {d['status'].replace('_', ' ').lower()}"
                      + (f" ({d['remarks']})" if d["remarks"] else "") for d in absences][:10],
            "link": {"page": "employees", "label": "Open attendance"}}  # fmt: skip
    return Routed(text, sm, [card], tool="attendance_details")


def _profile(ctx: ToolContext, r: Route) -> Routed:
    p = insights.employee_profile(ctx.session, ctx.user, r.employee or ctx.user.employee_code)
    text = (f"{p['full_name']} ({p['employee_code']}) is {p['designation'] or 'an employee'} in "
            f"{p['department'] or 'no department'}, {p['location'] or p['work_state']}, joined "
            f"{p['date_of_joining']}" + (f", reports to {p['manager']}" if p["manager"] else "")
            + ".")
    labels = {"designation": "Designation", "department": "Department", "location": "Location",
              "date_of_joining": "Joined", "manager": "Manager", "status": "Status",
              "tax_regime": "Tax regime"}
    card = {"type": "info_card", "title": f"{p['full_name']} · {p['employee_code']}",
            "fields": [{"label": v, "value": str(p[k] or "—")} for k, v in labels.items()],
            "link": {"page": "employees", "label": "Open in Employees"}}  # fmt: skip
    return Routed(text, p, [card], tool="employee_profile")


def _payslip(ctx: ToolContext, r: Route) -> Routed:
    employee = insights.resolve_employee(ctx.session, ctx.user, _who(r))
    month = r.month or _latest_month(ctx, employee)
    run = ctx.session.scalar(select(PayrollRun).where(PayrollRun.employee_id == employee.id,
                                                      PayrollRun.period == str(month)))
    if run is None:
        return Routed(f"Payroll for {employee.full_name} in {month.label} is not processed yet.")
    slip = run.payslip
    text = (f"{employee.full_name}'s {month.label} payslip: gross {_inr(slip['gross_earnings'])},"
            f" deductions {_inr(slip['total_deductions'])}, net pay {_inr(slip['net_pay'])}.")
    # the PDF link serves the logged-in user's own payslip only
    card = {"type": "payslip_card", "payslip": slip,
            "download_url": f"/payslips/{run.period}/pdf" if employee.id == ctx.user.id else None}
    return Routed(text, {"net_pay": slip["net_pay"]}, [card], tool="employee_payslip")


# ---------------------------------------------------------------------- writes (preview)


def _preview(ctx: ToolContext, r: Route) -> Routed:
    from payroll_copilot.services.simulation import PayrollChange, simulate

    kind = {"update_lop": "lop", "add_overtime": "overtime", "update_attendance": "attendance"
            }.get(r.intent, "adjustment")  # fmt: skip
    if not has(ctx.user, change_perm(kind)):
        return Routed("Your role cannot request this payroll change. Please contact HR."
                      if is_staff(ctx.user) else
                      "Only HR can request payroll changes. Please contact HR.")
    if r.employee is None:
        return Routed("Which employee? Please give the name or employee code, e.g. "
                      "'EMP-101 ka LOP 2 days kar do'.")
    employee = insights.resolve_employee(ctx.session, ctx.user, r.employee)
    assumptions: list[str] = []
    month = r.month
    if month is None and r.intent != "update_salary":
        month = _latest_attendance_month(ctx, employee)
        assumptions.append(f"Month not given: using {month.label} (latest attendance)")
    change: dict[str, Any] = {"reason": "Requested in chat"}
    if r.intent == "update_lop":
        change["lop_days"] = r.days
        title = f"LOP → {r.days} days"
    elif r.intent == "add_overtime":
        day = min(month.end, ctx.today)
        assumptions.append(f"Work date not given: using {day.isoformat()}")
        change["overtime"] = [{"work_date": day, "hours": r.hours}]
        title = f"Overtime +{r.hours} h"
    elif r.intent in ("add_bonus", "add_miscellaneous"):
        deduction = "deduction" in r.flags
        head = ("BONUS" if r.intent == "add_bonus" else "OTHER_DED" if deduction
                else "OTHER_EARNING_NT" if "non_taxable" in r.flags else "OTHER_EARNING")
        if r.intent == "add_miscellaneous" and not deduction:
            assumptions.append("Taxable earning" if head == "OTHER_EARNING"
                               else "Non-taxable earning")  # fmt: skip
        change["adjustments"] = [{"kind": "DEDUCTION" if deduction else "EARNING",
                                  "head_code": head, "amount": r.amount}]
        title = f"{insights.heads_name(head)} {'−' if deduction else '+'}{_inr(r.amount)}"
    elif r.intent == "update_attendance":
        change["attendance"] = r.attendance
        names = {"PRESENT": "Present", "ABSENT": "Absent", "HALF_DAY": "Half day",
                 "WEEK_OFF": "Week off", "HOLIDAY": "Holiday"}  # fmt: skip
        title = "Attendance: " + ", ".join(f"{a['date'][8:]} {month.label[:3]} → "
                                           f"{names[a['status']]}" for a in r.attendance)
        if "with_lop" in r.flags and r.attendance[0]["status"] in ("ABSENT", "HALF_DAY"):
            from payroll_copilot.repositories.attendance_repo import AttendanceRepository

            current = AttendanceRepository(ctx.session).lop_record(employee.id, month)
            per_day = Decimal("0.5") if r.attendance[0]["status"] == "HALF_DAY" else Decimal(1)
            change["lop_days"] = (current.days if current else ZERO) + per_day * len(r.attendance)
            title += f" with LOP {change['lop_days']} days"
        else:
            assumptions.append("LOP not changed (add 'with LOP' / 'LOP bhi' to apply LOP)")
    elif r.intent == "update_salary":
        gross = r.amount
        if "annual" in r.flags:
            gross = (r.amount / 12).quantize(Decimal(1))
            assumptions.append(f"{_inr(r.amount)} a year = {_inr(gross)} monthly gross")
        effective = (r.month or PayrollPeriod.from_date(ctx.today)).start
        if r.month is None:
            assumptions.append(f"Effective date not given: using {effective.isoformat()}")
        month = PayrollPeriod.from_date(effective)
        change["salary"] = {"monthly_gross": gross, "effective_from": effective}
        title = f"Salary → {_inr(gross)} monthly gross from {effective:%d %b %Y}"
    else:
        return Routed("That change is not supported yet.")
    try:
        result = simulate(ctx.session, ctx.user, employee.employee_code, month,
                          PayrollChange.model_validate(change))  # fmt: skip
    except NotFoundError as exc:
        if r.intent != "update_salary":
            return Routed(f"I can't prepare that change for {employee.full_name}: {exc}")
        # a future month has no attendance yet: show the structure change (engine breakup)
        result = _structure_preview(ctx, employee, change["salary"]["effective_from"],
                                    change["salary"]["monthly_gross"])  # fmt: skip
        if result is None:
            return Routed(f"I can't prepare that change for {employee.full_name}: {exc}")
        assumptions.append(f"Payroll for {month.label} can't be calculated yet ({exc}); "
                           "showing the salary structure change only")
    except (PayrollCopilotError, ValueError) as exc:
        return Routed(f"I can't prepare that change for {employee.full_name}: {exc}")
    net = Decimal(result["net_change"])
    what = "Monthly gross" if result.get("structure_only") else "Net pay"
    text = (f"Preview for {employee.full_name}, {month.label}: {title}. {what} "
            f"{'+' if net >= 0 else '−'}{_inr(abs(net))}. Nothing has been changed - "
            "press Review & Apply to give a reason and confirm.")
    proposal = {"employee_code": employee.employee_code, "month": str(month),
                "change": PayrollChange.model_validate(change).model_dump(mode="json")}
    card = {"type": "action_preview", "intent": r.intent, "title": title,
            "assumptions": assumptions, "proposal": proposal, **result}
    return Routed(text, {}, [card], tool="simulate")


def _structure_preview(ctx: ToolContext, employee: Employee, effective: Any,
                       gross: Decimal) -> dict[str, Any] | None:  # fmt: skip
    """Old vs new salary structure for a revision (no payroll month needed)."""
    from datetime import timedelta

    from payroll_copilot.services.salary_structures import preview

    code = ctx.session.scalar(select(SalaryStructure.template_code).where(
        SalaryStructure.employee_id == employee.id, SalaryStructure.effective_from <= effective,
        SalaryStructure.template_code.is_not(None))
        .order_by(SalaryStructure.effective_from.desc()))  # fmt: skip
    if code is None:
        return None
    old = insights.structure_on(ctx.session, employee, effective - timedelta(days=1))
    new = {ln["head_code"]: Decimal(ln["amount"]) for ln in preview(code, str(gross))["lines"]}
    rows = [{"key": f"earnings:{h}", "label": f"Earning: {insights.heads_name(h)}",
             "unit": "INR", "before": str(old.get(h, ZERO)), "after": str(new.get(h, ZERO)),
             "difference": str(new.get(h, ZERO) - old.get(h, ZERO))}
            for h in dict.fromkeys([*old, *new])]  # fmt: skip
    old_gross, new_gross = sum(old.values(), ZERO), sum(new.values(), ZERO)
    rows.insert(0, {"key": "gross", "label": "Monthly gross (structure)", "unit": "INR",
                    "before": str(old_gross), "after": str(new_gross),
                    "difference": str(new_gross - old_gross)})
    return {"employee_code": employee.employee_code, "employee_name": employee.full_name,
            "period": str(PayrollPeriod.from_date(effective)),
            "period_label": PayrollPeriod.from_date(effective).label, "rows": rows,
            "net_change": str(new_gross - old_gross), "overtime": [], "adjustments": [],
            "warnings": [], "saved": False, "structure_only": True}  # fmt: skip


AUDIT_SYNC_LIMIT = 300  # bigger companies run the audit from the AI Audit page (background)


def _audit(ctx: ToolContext, r: Route) -> Routed:
    """Latest audit of the month (from today), else run one now - then answer the question."""
    from datetime import UTC, datetime, timedelta

    from payroll_copilot.services import payroll_audit as pa

    if not is_staff(ctx.user):
        return Routed("Only HR can run the payroll audit.")
    month = r.month or _latest_processed_month(ctx)
    row = pa.latest(ctx.session, month)
    fresh = row is not None and row.finished_at and (
        datetime.now(UTC) - (row.finished_at if row.finished_at.tzinfo
                             else row.finished_at.replace(tzinfo=UTC))) < timedelta(days=1)
    if not fresh:
        employees = ctx.session.scalar(select(func.count()).select_from(PayrollRun).where(
            PayrollRun.period == str(month))) or 0
        if employees == 0:
            return Routed(f"No payroll is processed for {month.label} yet, so there is nothing "
                          "to audit.")
        if employees > AUDIT_SYNC_LIMIT:
            return Routed(f"{employees} payslips in {month.label}: start the audit from the AI "
                          "Audit page (it runs in the background).")
        row = pa.start(ctx.session, ctx.user, month)
        pa._execute(ctx.session, row)
        row.status, row.finished_at = "COMPLETED", datetime.now(UTC)
        ctx.session.flush()
    low_topic = {"PF": "PF mismatch", "ESIC": "ESIC mismatch", "PT": "PT mismatch",
                 "TDS": "TDS mismatch"}.get(r.component or "")
    issues = [*row.findings, *row.anomalies]
    wanted = [i for i in issues if low_topic is None or i["issue"] == low_topic]
    if "arrears" in r.flags:
        low_topic = "negative arrears"
        wanted = [i for i in issues if "arrear" in i["issue"].lower()]
    s = row.summary
    topic = low_topic or "issue"
    text = (f"AI audit of {month.label} ({s['employees']} employees, health "
            f"{row.health['score']}%): {s['passed']} passed, {s['warnings']} with warnings, "
            f"{s['failed']} failed.")
    if low_topic:
        text += (f" {topic} for {len(wanted)} employee{'s' if len(wanted) != 1 else ''}."
                 if wanted else f" No {topic} found.")
    elif s["by_issue"]:
        text += " Found: " + ", ".join(f"{n} × {k}" for k, n in s["by_issue"].items()) + "."
    causes = Counter(c for i in wanted for c in i.get("causes", [])[:1])
    if causes:
        top, n = causes.most_common(1)[0]
        text += f" Main root cause ({n} employee{'s' if n != 1 else ''}): {top}"
    notes = [f"{i['employee_name']} ({i['employee_code']}): {i['issue']} - "
             + (f"payslip {i['actual']}, expected {i['expected']}" if i.get("expected") else
                i.get("message", "")) for i in wanted[:8]]
    card = {"type": "info_card", "title": f"AI Audit · {month.label}",
            "subtitle": f"Health {row.health['score']}% · checked {iso_when(row.finished_at)}",
            "fields": [{"label": c["category"], "value": {"PASS": "✓ OK", "WARNING": "⚠ Warning",
                                                           "FAIL": "✗ Failed"}[c["status"]],
                        "hint": f"{c['passed']} ok · {c['warnings']} warning · "
                                f"{c['failed']} failed"}
                       for c in row.health["categories"]],
            "notes": notes, "link": {"page": "ai-audit", "label": "Open AI Audit"}}  # fmt: skip
    return Routed(text, s, [card], tool="payroll_audit")


def iso_when(value: Any) -> str:
    return value.strftime("%d %b %H:%M UTC") if value else ""


def _latest_processed_month(ctx: ToolContext) -> PayrollPeriod:
    period = ctx.session.scalar(select(PayrollRun.period).order_by(PayrollRun.period.desc()))
    return PayrollPeriod.parse(period) if period else PayrollPeriod.from_date(ctx.today)


def _run_payroll(ctx: ToolContext, r: Route) -> Routed:
    from payroll_copilot.services.payroll_runs import prechecks

    if not has(ctx.user, Perm.RUN_PAYROLL):
        return Routed("Only HR can run payroll.")
    month = r.month or PayrollPeriod.from_date(ctx.today)
    pc = prechecks(ctx.session, month)
    s = pc["summary"]
    text = (f"Pre-checks for {pc['label']}: {pc['employees']} employees, {s['passed']} checks "
            f"passed, {s['warnings']} with warnings, {s['failed']} failed"
            + (f"; {pc['already_processed']} already processed" if pc["already_processed"]
               else "") + ". Start payroll from Payroll Runs, where you review LOP and confirm.")
    card = {"type": "info_card", "title": f"Payroll readiness · {pc['label']}",
            "subtitle": f"{pc['employees']} employees in scope",
            "fields": [{"label": c["label"], "value": {"PASS": "✓ OK", "WARNING": "⚠ Warning",
                                                       "FAIL": "✗ Failed"}[c["status"]],
                        "hint": f"{c['passed']} ok · {c['warnings']} warning · "
                                f"{c['failed']} failed"}
                       for c in pc["checks"]],
            "link": {"page": "payroll-runs", "label": "Open Payroll Runs"}}  # fmt: skip
    return Routed(text, {}, [card], tool="prechecks")


def _knowledge(ctx: ToolContext, r: Route) -> Routed:
    from payroll_copilot.services.knowledge import answer

    a = answer(ctx.session, ctx.user, r.text, ctx.today)
    if not a["found"]:
        return Routed(a["text"], tool="knowledge_search")
    card = {"type": "knowledge_card", "question": r.text, "passages": [
        {k: p[k] for k in ("title", "section", "snippet", "source", "status", "jurisdiction",
                           "effective_from", "effective_to", "document_id")}
        for p in a["passages"]]}  # fmt: skip
    return Routed(a["text"], {}, [card], tool="knowledge_search")


def _apply_leave(ctx: ToolContext, r: Route) -> Routed:
    """A leave application card: checked now, sent only when the user presses Apply."""
    from datetime import date as _date

    from payroll_copilot.services import leave_requests as leaves

    if r.employee and r.employee != ctx.user.employee_code:
        return Routed("Leave is applied by the employee themself (AI Chat or the Leave page). "
                      "To change someone's attendance, use Employees > Attendance.")
    form = {"leave_type": r.extra.get("leave_type") or "CL", "from": r.extra.get("from"),
            "to": r.extra.get("to"), "reason": r.extra.get("reason") or ""}
    card: dict[str, Any] = {"type": "leave_request_card", "form": form, "plan": None,
                            "error": None, "type_guessed": not r.extra.get("leave_type")}
    if not form["from"]:
        card["error"] = "Pick the dates."
        return Routed("Which dates? For example 'CL on 20 Oct' or 'sick leave 20 to 22 Oct'. "
                      "You can also fill the card below.", {}, [card], tool="leave_request")
    try:
        plan = leaves.plan(ctx.session, ctx.user, form["leave_type"],
                           _date.fromisoformat(form["from"]), _date.fromisoformat(form["to"]),
                           today=ctx.today)  # fmt: skip
    except PayrollCopilotError as exc:
        card["error"] = str(exc)
        return Routed(f"I can't apply that leave: {exc}", {}, [card], tool="leave_request")
    card["plan"] = plan
    span = (plan["from"] if plan["from"] == plan["to"] else f"{plan['from']} to {plan['to']}")
    text = f"{plan['leave_label']}, {span}: {plan['days']} working day(s)."
    if plan["balance"]:
        text += f" Balance {plan['balance']['available']} -> {plan['balance']['after']}."
    if plan["skipped"]:
        text += f" Not counted: {len(plan['skipped'])} week off / holiday day(s)."
    who = " or ".join(plan["approvers"][:2]) or "your manager / HR"
    text += f" Check the card and press Apply - it goes to {who} for approval."
    return Routed(text, {k: plan[k] for k in ("leave_type", "from", "to", "days")}, [card],
                  tool="leave_request")  # fmt: skip


def _tax_help(ctx: ToolContext, r: Route) -> Routed:
    from payroll_copilot.services.tax_help import compare_regimes

    employee = (directory._get_visible(ctx.session, ctx.user, r.employee) if r.employee
                else ctx.user)
    month = PayrollPeriod.from_date(ctx.today)
    data = compare_regimes(ctx.session, ctx.user, employee, month, r.extra.get("declared"))
    old, new = data["regimes"]
    if not (old["available"] and new["available"]):
        missing = old if not old["available"] else new
        return Routed(f"I can't compare the regimes: {missing['reason']}", data, tool="tax_help")
    who = "Your" if employee.id == ctx.user.id else f"{employee.full_name}'s"
    text = (f"{who} projected taxable salary for FY {data['financial_year']} is "
            f"{_inr(old['gross'])}. Old regime tax: {_inr(old['total_tax'])}"
            + (f" (with {', '.join(f'{k} {_inr(v)}' for k, v in data['declared'].items())})"
               if data["declared"] else " (no deductions declared)")
            + f"; new regime tax: {_inr(new['total_tax'])}. ")  # fmt: skip
    if data["better"] == "SAME":
        text += "Both regimes give the same tax."
    else:
        text += (f"The {data['better'].lower()} regime is lower by {_inr(data['saving'])} "
                 f"(current regime: {data['current_regime'].lower()}).")
    if data["tips"] and data["better"] == "NEW":
        maxed, new_tax = Decimal(data["old_tax_all_used"]), Decimal(new["total_tax"])
        text += (f" Even using every old-regime deduction limit, the old regime would be "
                 f"{_inr(maxed)}" + (" - lower than the new regime." if maxed < new_tax else
                                     " - the same as the new regime." if maxed == new_tax
                                     else " - still higher."))  # fmt: skip
    elif data["tips"]:
        best = max(data["tips"], key=lambda t: Decimal(t["tax_saved"]))
        text += (f" Unused {best['section']} limit {_inr(best['unused'])} would cut the old-regime "
                 f"tax by {_inr(best['tax_saved'])}.")
    text += " " + data["disclaimer"]
    return Routed(text, {k: data[k] for k in ("financial_year", "better", "saving")},
                  [{"type": "tax_compare_card", **data}], tool="tax_help")  # fmt: skip


READERS = {
    "payroll_explanation": _explain, "payroll_comparison": _compare,
    "compliance_question": _rules, "salary_lookup": _salary, "payroll_lookup": _salary,
    "attendance_lookup": _attendance, "employee_lookup": _profile, "payslip_lookup": _payslip,
    "knowledge_question": _knowledge, "tax_help": _tax_help,
}


def handle(ctx: ToolContext, r: Route) -> Routed | None:
    """The answer for a routed message, or None to let the LLM tool loop answer."""
    if r.kind not in (READ, WRITE, PENDING):
        return None
    if r.employee_error:
        return Routed(r.employee_error)
    try:
        if r.kind == PENDING:
            return Routed(PENDING_TEXT.get(r.intent, "That is not available yet."))
        if r.intent == "payroll_audit":
            return _audit(ctx, r)
        if r.intent == "run_payroll":
            return _run_payroll(ctx, r)
        if r.intent == "apply_leave":
            return _apply_leave(ctx, r)
        if r.kind == WRITE:
            return _preview(ctx, r)
        reader = READERS.get(r.intent)
        return reader(ctx, r) if reader else None
    except PermissionDeniedError as exc:
        return Routed(f"{exc}.")
    except NotFoundError as exc:
        return Routed(f"{exc}.")
    except PayrollCopilotError as exc:
        return Routed(f"I couldn't answer that: {exc}")

