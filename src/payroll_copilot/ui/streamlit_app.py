"""AI Payroll Copilot - Streamlit UI (talks only to the local FastAPI backend).

Run:  payroll-copilot ui --with-api     (or: payroll-copilot api serve  +  payroll-copilot ui)
"""

from uuid import uuid4

import pandas as pd
import streamlit as st

from payroll_copilot.core.config import get_settings
from payroll_copilot.ui import formatting as fmt
from payroll_copilot.ui.api_client import ApiClient, ApiError

st.set_page_config(page_title="AI Payroll Copilot", page_icon="🧾", layout="wide")


def get_client() -> ApiClient:
    if "client" not in st.session_state:
        settings = get_settings()
        key = settings.api_key.get_secret_value() if settings.api_key else None
        st.session_state.client = ApiClient(settings.api_base_url, key)
    return st.session_state.client


def call(fn, *args, **kwargs):
    """Run an API call; show the error instead of crashing the page."""
    try:
        return fn(*args, **kwargs)
    except ApiError as exc:
        if exc.status == 404:
            st.info(exc.detail)
        else:
            st.error(f"API error {exc.status or ''}: {exc.detail}")
        return None


def show_text(text: str) -> None:
    body, *warnings = text.split("\nWARNING: ")
    st.code(body, language=None)
    for warning in warnings:
        st.warning(warning.strip())


def show_traces(data: dict) -> None:
    for trace in fmt.collect_traces(data):
        with st.expander(f"🔎 Calculation trace · {fmt.trace_header(trace)}"):
            st.caption(f"Formula: {trace.get('formula', '')}")
            st.dataframe(pd.DataFrame(fmt.trace_rows(trace)), hide_index=True,
                         width="stretch")  # fmt: skip
            for warning in trace.get("warnings", []):
                st.caption(f"⚠️ {warning}")


api = get_client()
state = st.session_state
state.setdefault("messages", [])
state.setdefault("thread_id", uuid4().hex[:12])

st.title("🧾 AI Payroll Copilot")
st.caption("Deterministic payroll engine · QA copilot · local demo data only · "
           "statutory rules are UNVERIFIED until checked")  # fmt: skip

# ---------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Context")
    health = call(api.health)
    if health is None:
        st.error("Start the API first:")
        st.code("payroll-copilot api serve", language=None)
        st.stop()
    st.success(f"API connected · DB {health['database']} · LLM {health['llm_provider']}")
    employees = call(api.employees) or []
    labels = {e["employee_code"]: f"{e['employee_code']} · {e['full_name']} ({e['work_state']})"
              for e in employees}  # fmt: skip
    directory = [(e["employee_code"], e["full_name"]) for e in employees]
    code = st.selectbox("Employee", list(labels), format_func=labels.get, key="employee")
    months = fmt.month_options()
    period = st.selectbox("Payroll month", months, index=months.index("2026-09"), key="period")
    use_context = st.toggle("Add selected employee & month to chat questions", value=True,
                            key="use_context")  # fmt: skip
    acting_as = st.text_input("Acting as (user name)", value="hr.user", key="acting_as",
                              help="Sent as X-User; approvals need a user from APPROVERS "
                                   "different from the requester")  # fmt: skip
    if st.button("🆕 New conversation", key="new_chat"):
        state.messages = []
        state.thread_id = uuid4().hex[:12]
    st.caption(f"Conversation id: {state.thread_id}")

chat_tab, calc_tab, eva_tab, qa_tab, kb_tab, approval_tab = st.tabs(
    ["💬 Chat", "🧮 Calculation", "⚖️ Expected vs Actual", "📋 QA Report", "📚 Policy",
     "✅ Approvals"]
)  # fmt: skip

# ---------------------------------------------------------------------- chat
with chat_tab:
    for message in state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["content"])
            else:
                st.text(message["content"])
                reply = message.get("reply") or {}
                if reply:
                    with st.expander("Details: route, tools, sources"):
                        st.write({"intent": reply["intent"], "route": reply["route"],
                                  "tools": reply["tools_used"], "used_llm": reply["used_llm"],
                                  "entities": {k: v for k, v in reply["entities"].items() if v}})
                        for check in reply.get("checks", []):
                            st.caption(f"✔ {check}")
                    for artifact in reply.get("artifacts", []):
                        show_traces(artifact)
                    if reply.get("pending_action"):
                        st.info("⏸️ Waiting for your confirmation - type **yes** to submit the "
                                "approval request or **no** to cancel.")  # fmt: skip

    prompt = st.chat_input("Ask: PF kitna hai? · Why was salary reduced? · Validate payroll…",
                           key="chat_input")  # fmt: skip
    if prompt:
        question = fmt.add_context(prompt, code, period, directory) if use_context else prompt
        state.messages.append({"role": "user", "content": question})
        reply = call(api.chat, question, state.thread_id, acting_as or None)
        if reply is not None:
            state.messages.append({"role": "assistant", "content": reply["answer"],
                                   "reply": reply})  # fmt: skip
        st.rerun()

# ---------------------------------------------------------------------- calculation
OPERATIONS = {
    "Expected payslip": ("view", "payslip"),
    "Salary / proration": ("calc", "salary"),
    "Attendance / paid days": ("view", "attendance"),
    "Salary structure": ("view", "salary-structure"),
    "PF / EPS / EDLI": ("calc", "pf"),
    "PF old vs new rule": ("calc", "pf-rules"),
    "ESIC": ("calc", "esic"),
    "Professional Tax": ("calc", "pt"),
    "TDS": ("calc", "tds"),
    "Arrears": ("calc", "arrear"),
    "Full & Final": ("calc", "fnf"),
}

with calc_tab:
    st.subheader(f"Calculation · {labels.get(code, code)} · {period}")
    choice = st.selectbox("What to calculate", list(OPERATIONS), key="calc_choice")
    if st.button("Calculate", key="calc_run", type="primary"):
        kind, name = OPERATIONS[choice]
        if kind == "view":
            result = call(api.employee_view, name, code, period)
        elif name == "fnf":
            result = call(api.calculate, "fnf", code)
        else:
            result = call(api.calculate, name, code, period)
        if result is not None:
            payslip = result["data"].get("payslip")
            if payslip:
                tables = fmt.payslip_tables(payslip)
                cols = st.columns(3)
                for col, title in zip(cols, ("Earnings", "Employee deductions",
                                             "Employer contributions"), strict=True):  # fmt: skip
                    col.markdown(f"**{title}**")
                    col.dataframe(pd.DataFrame(tables[title]), hide_index=True,
                                  width="stretch")  # fmt: skip
                st.dataframe(pd.DataFrame(tables["Totals"]), hide_index=True)
            with st.expander("Engine output", expanded=payslip is None):
                show_text(result["text"])
            show_traces(result["data"])

# ---------------------------------------------------------------------- expected vs actual
with eva_tab:
    st.subheader(f"Expected vs Actual · {labels.get(code, code)} · {period}")
    only_issues = st.toggle("Show only issues", value=False, key="eva_only_issues")
    if st.button("Compare", key="eva_run", type="primary"):
        result = call(api.validate_employee, code, period)
        if result is not None:
            validation = result["data"]["validation"]
            rows = fmt.comparison_rows(validation, only_issues=only_issues)
            issues = [r for r in validation["results"]
                      if r["status"] in ("MISMATCH", "MISSING_ACTUAL", "MISSING_EXPECTED")]
            c1, c2 = st.columns(2)
            c1.metric("Components compared", len(validation["results"]))
            c2.metric("Issues", len(issues))
            if validation.get("note"):
                st.info(validation["note"] + " - create it with `payroll-copilot db seed-actual`")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

# ---------------------------------------------------------------------- QA report
with qa_tab:
    st.subheader(f"Payroll QA · {period}")
    if st.button("Run QA for the month", key="qa_run", type="primary"):
        result = call(api.validate_payroll, period)
        if result is not None:
            summary, findings = result["data"]["summary"], result["data"]["findings"]
            cols = st.columns(4)
            cols[0].metric("Employees checked", summary["employees_checked"])
            cols[1].metric("Clean", summary["clean"])
            cols[2].metric("With issues", summary["with_issues"])
            cols[3].metric("Findings", summary["findings"])
            st.caption(f"Net pay overpaid {fmt.inr(summary['net_overpaid'])} · underpaid "
                       f"{fmt.inr(summary['net_underpaid'])} · from "
                       f"{summary['component_issues']} component mismatches")  # fmt: skip
            if summary["by_category"]:
                st.bar_chart(pd.Series(summary["by_category"], name="findings"))
            st.dataframe(pd.DataFrame(fmt.finding_rows(findings)), hide_index=True,
                         width="stretch")  # fmt: skip
            st.download_button("⬇️ Download findings (CSV)", fmt.findings_csv(period, findings),
                               file_name=f"payroll_qa_{period}.csv", mime="text/csv",
                               key="qa_download")  # fmt: skip
    runs = call(api.qa_runs) or []
    if runs:
        st.markdown("**Saved QA runs** (saved with `payroll-copilot qa run --save`)")
        st.dataframe(pd.DataFrame([{"Run": r["id"], "Period": r["period"],
                                    "Findings": r["findings"], "Created": r["created_at"]}
                                   for r in runs]), hide_index=True)  # fmt: skip

# ---------------------------------------------------------------------- approvals
with approval_tab:
    st.subheader("Approvals (maker-checker)")
    st.caption(f"Acting as **{acting_as or '-'}**. Write actions run only after a different "
               "user listed in APPROVERS approves them.")  # fmt: skip
    pending = call(api.approvals, "PENDING") or []
    if not pending:
        st.info("No pending approval requests.")
    for item in pending:
        with st.container(border=True):
            st.markdown(f"**#{item['id']} · {item['description']}**  \n"
                        f"requested by `{item['requested_by']}` · {item['reason'] or ''}")
            if item.get("preview"):
                st.json(item["preview"], expanded=False)
            comment = st.text_input("Comment", key=f"comment_{item['id']}")
            c1, c2 = st.columns(2)
            if c1.button("✅ Approve", key=f"approve_{item['id']}"):
                done = call(api.decide, item["id"], True, acting_as, comment or None)
                if done:
                    st.success(f"#{item['id']} {done['status']}: {done.get('result')}")
            if c2.button("❌ Reject", key=f"reject_{item['id']}"):
                done = call(api.decide, item["id"], False, acting_as, comment or None)
                if done:
                    st.warning(f"#{item['id']} {done['status']}")
    history = [a for a in (call(api.approvals) or []) if a["status"] != "PENDING"]
    if history:
        st.markdown("**Decided requests**")
        st.dataframe(pd.DataFrame([{"#": a["id"], "Status": a["status"],
                                    "Action": a["description"], "Requested by": a["requested_by"],
                                    "Decided by": a["decided_by"], "Result": str(a["result"])}
                                   for a in history]), hide_index=True, width="stretch")

# ---------------------------------------------------------------------- policy search
with kb_tab:
    st.subheader("Policy & rule knowledge base")
    query = st.text_input("Question", "What is the PF wage ceiling?", key="kb_query")
    c1, c2, c3 = st.columns(3)
    module = c1.selectbox("Module", ["", "pf", "esic", "pt", "lwf", "tds", "payroll",
                                     "attendance", "arrears", "fnf", "compliance"],
                          key="kb_module")  # fmt: skip
    in_period = c2.toggle(f"Only rules effective in {period}", value=True, key="kb_in_period")
    kb_state = c3.text_input("State (PT/LWF)", "", key="kb_state").strip().upper() or None
    if st.button("Search", key="kb_run", type="primary"):
        result = call(api.kb_search, query, module=module or None,
                      period=period if in_period else None, state=kb_state, k=4)  # fmt: skip
        if result is not None:
            if not result["results"]:
                st.info("Nothing effective for these filters.")
            for number, hit in enumerate(result["results"], start=1):
                with st.expander(f"[{number}] {hit['citation']}", expanded=number == 1):
                    st.markdown(hit["text"])
