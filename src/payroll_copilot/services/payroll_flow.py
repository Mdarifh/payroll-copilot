"""Payroll run as a LangGraph workflow that waits for HR (human in the loop).

    START -> prechecks -> review -> approval --approve--> run -> running --> validate
                ^                    |  ^                         |  ^          |
                +------ recheck -----+  +-- (blockers) --+        +--+ (still   v
                                     |                             running)  summary -> END
                                     +-- cancel --> cancelled -> END

* prechecks - the same checks as the Payroll Runs page; a FAILED check blocks the run.
* review    - creates the bulk payroll batch with the LOP review rows (batch_service).
* approval  - ``interrupt``: the graph stops and the chat shows the approval card. HR edits
              LOP in the card, then resumes with approve (mode + reason), recheck or cancel.
* run       - applies LOP and queues the batch (one conditional UPDATE: never twice).
* running   - ``interrupt`` while the background worker calculates; the card polls the
              progress and resumes with "continue" when the batch has finished.
* validate  - all payroll checks for the batch's employees.
* summary   - totals and the next step (approve / finalize on the Payroll Runs page:
              maker-checker, a second person approves).

The graph state is saved by a LangGraph checkpointer (SQLite file ``data/checkpoints.db``),
so a paused run survives an API restart and can be resumed later. The payroll data itself
is written only by the existing services, each step in its own committed transaction.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError, PayrollCopilotError
from payroll_copilot.db.models import BatchItem, Employee, PayrollRun
from payroll_copilot.services import audit, batch_service

STAGES = ["prechecks", "review", "approval", "run", "validate", "summary"]


class FlowState(TypedDict, total=False):
    month: str
    department: str | None
    work_state: str | None
    started_by: str  # employee code
    prechecks: dict[str, Any]
    blockers: list[dict[str, Any]]
    batch_id: str | None
    decision: dict[str, Any]
    run: dict[str, Any]
    validation: dict[str, Any]
    totals: dict[str, Any]
    outcome: str  # done | cancelled
    error: str | None


@dataclass
class FlowContext:
    session: Session
    actor: Employee


def _compact_prechecks(pc: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """What the card shows: each check's status and its failed employees (blockers)."""
    checks, blockers = [], []
    for c in pc["checks"]:
        checks.append({k: c[k] for k in ("key", "label", "status", "passed", "warnings",
                                          "failed")})  # fmt: skip
        if c["status"] == "FAIL":
            failed = [i for i in c.get("issues", []) if i.get("severity") == "ERROR"]
            blockers.append({"check": c["label"], "count": c["failed"], "employees": [
                {"code": i["employee_code"], "name": i["employee_name"], "message": i["message"]}
                for i in failed[:10]]})  # fmt: skip
    notes = [n["message"] for c in pc["checks"] for n in c.get("notes", [])]
    return ({"label": pc["label"], "employees": pc["employees"], "summary": pc["summary"],
             "already_processed": pc["already_processed"], "checks": checks,
             "notes": notes}, blockers)  # fmt: skip


class PayrollFlow:
    """Start, inspect and resume payroll runs. One instance per API process."""

    def __init__(self, session_factory: sessionmaker, checkpointer: BaseCheckpointSaver,
                 wake_worker: Callable[[], None] | None = None) -> None:  # fmt: skip
        self.session_factory = session_factory
        self.wake_worker = wake_worker
        self.graph = self._build().compile(checkpointer=checkpointer)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ graph

    def _build(self) -> StateGraph:
        g = StateGraph(FlowState, context_schema=FlowContext)
        for name in ("prechecks", "review", "approval", "run", "running", "validate",
                     "summary", "cancelled"):  # fmt: skip
            g.add_node(name, getattr(self, f"_{name}"))
        g.add_edge(START, "prechecks")
        g.add_edge("prechecks", "review")
        g.add_edge("review", "approval")
        g.add_edge("run", "running")
        g.add_edge("validate", "summary")
        g.add_edge("summary", END)
        g.add_edge("cancelled", END)
        return g  # approval / running choose their next node with Command(goto=...)

    def _prechecks(self, state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
        from payroll_copilot.services.payroll_runs import prechecks

        pc = prechecks(runtime.context.session, PayrollPeriod.parse(state["month"]),
                       state.get("department"))  # fmt: skip
        compact, blockers = _compact_prechecks(pc)
        return {"prechecks": compact, "blockers": blockers, "error": None}

    def _review(self, state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
        if state.get("blockers"):
            return {}  # nothing can run yet: the approval card shows what to fix
        session = runtime.context.session
        if state.get("batch_id"):  # a re-check keeps the review HR already edited
            batch = batch_service.get_batch(session, state["batch_id"])
            if batch.status.value == "REVIEW":
                return {}
        batch = batch_service.start_review(session, runtime.context.actor,
                                           PayrollPeriod.parse(state["month"]),
                                           state.get("department"), state.get("work_state"))
        return {"batch_id": batch.batch_id}

    def _approval(self, state: FlowState, runtime: Runtime[FlowContext]) -> Command:
        decision = interrupt({"stage": "approval"})
        action = decision.get("action")
        if action == "cancel":
            return Command(goto="cancelled", update={"decision": decision})
        if action == "recheck":
            return Command(goto="prechecks", update={"decision": decision})
        if action != "approve":
            return Command(goto="approval", update={"error": f"Unknown action {action!r}"})
        if state.get("blockers"):
            return Command(goto="approval", update={
                "error": "Fix the failed pre-checks first, then press Re-check."})
        return Command(goto="run", update={"decision": decision, "error": None})

    def _run(self, state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
        d = state["decision"]
        batch = batch_service.request_run(runtime.context.session, state["batch_id"],
                                          runtime.context.actor, d.get("mode", "APPLY"),
                                          d.get("reason"))  # fmt: skip
        return {"run": batch_service.status(runtime.context.session, batch)}

    def _running(self, state: FlowState, runtime: Runtime[FlowContext]) -> Command:
        interrupt({"stage": "running"})
        session = runtime.context.session
        status = batch_service.status(session, batch_service.get_batch(session,
                                                                       state["batch_id"]))
        if not status["finished"]:
            return Command(goto="running", update={"run": status})
        return Command(goto="validate", update={"run": status})

    def _validate(self, state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
        try:
            result = batch_service.validate(runtime.context.session, state["batch_id"],
                                            runtime.context.actor)  # fmt: skip
        except PayrollCopilotError as exc:  # e.g. nobody was processed
            result = {"errors": 0, "warnings": 0, "passed": 0, "stage": None, "note": str(exc)}
        return {"validation": result}

    def _summary(self, state: FlowState, runtime: Runtime[FlowContext]) -> dict[str, Any]:
        session = runtime.context.session
        batch = batch_service.get_batch(session, state["batch_id"])
        gross, net, people = session.execute(
            select(func.coalesce(func.sum(PayrollRun.gross_earnings), 0),
                   func.coalesce(func.sum(PayrollRun.net_pay), 0), func.count(PayrollRun.id))
            .join(BatchItem, BatchItem.employee_id == PayrollRun.employee_id)
            .where(BatchItem.batch_pk == batch.id, PayrollRun.period == batch.period)).one()
        return {"outcome": "done", "totals": {"employees": people,
                                              "gross": str(Decimal(gross)),
                                              "net": str(Decimal(net))}}  # fmt: skip

    def _cancelled(self, state: FlowState) -> dict[str, Any]:
        return {"outcome": "cancelled"}

    # ------------------------------------------------------------------ public

    @staticmethod
    def _config(flow_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"payroll-flow:{flow_id}"}}

    def _lock(self, flow_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(flow_id, threading.Lock())

    def _invoke(self, flow_id: str, actor_id: int, payload: Any) -> dict[str, Any]:
        """Run the graph until it pauses or ends, in one committed transaction."""
        with self._lock(flow_id), self.session_factory() as session:
            actor = session.get(Employee, actor_id)
            try:
                self.graph.invoke(payload, self._config(flow_id),
                                  context=FlowContext(session=session, actor=actor))
                session.commit()
            except Exception:
                session.rollback()
                raise
        if self.wake_worker is not None:
            self.wake_worker()  # a queued batch is visible to the worker only after commit
        return self.card(flow_id)

    def start(self, actor: Employee, month: PayrollPeriod, department: str | None = None,
              work_state: str | None = None) -> dict[str, Any]:  # fmt: skip
        flow_id = uuid4().hex[:16]
        card = self._invoke(flow_id, actor.id, {
            "month": str(month), "department": department, "work_state": work_state,
            "started_by": actor.employee_code})  # fmt: skip
        self._audit(actor, flow_id, "payroll_flow.start", {"month": str(month),
                                                           "department": department,
                                                           "work_state": work_state})
        return card

    def resume(self, actor: Employee, flow_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        card = self.card(flow_id)
        action = decision.get("action")
        if card["stage"] == "error" and action == "retry":
            return self._invoke(flow_id, actor.id, None)  # run the failed step again
        if card["stage"] not in ("approval", "running"):
            return card  # already finished: resuming again changes nothing
        if card["stage"] == "running" and action != "continue":
            raise PayrollCopilotError("Payroll is running - wait until it has finished")
        safe = {k: decision.get(k) for k in ("action", "mode", "reason") if decision.get(k)}
        if action == "approve" and not card["blockers"]:
            self._check_reason(card["batch_id"], safe)
        result = self._invoke(flow_id, actor.id, Command(resume=safe))
        if safe.get("action") != "continue":
            self._audit(actor, flow_id, "payroll_flow.decision", safe)
        return result

    def _check_reason(self, batch_id: str, decision: dict[str, Any]) -> None:
        """Refuse an approval the run step would refuse, before the graph moves on."""
        from payroll_copilot.db.models import BatchLopItem

        if decision.get("mode", "APPLY") != "APPLY" or len(decision.get("reason") or "") >= 5:
            return
        with self.session_factory() as session:
            batch = batch_service.get_batch(session, batch_id)
            applying = session.scalar(select(func.count()).select_from(BatchLopItem).where(
                BatchLopItem.batch_pk == batch.id, BatchLopItem.apply.is_(True),
                BatchLopItem.lop_days > 0))  # fmt: skip
        if applying:
            raise PayrollCopilotError("A reason (at least 5 characters) is required to apply LOP")

    def card(self, flow_id: str) -> dict[str, Any]:
        """The chat card for the flow's current state (also after an API restart)."""
        snapshot = self.graph.get_state(self._config(flow_id))
        state: FlowState = snapshot.values
        if not state:
            raise NotFoundError(f"Payroll run {flow_id} not found")
        waiting = [i.value for t in snapshot.tasks for i in t.interrupts]
        failed = [t.error for t in snapshot.tasks if t.error]
        if waiting:
            stage = waiting[0]["stage"]
        elif snapshot.next:  # a step raised an error: it can be retried
            stage = "error"
        else:
            stage = state.get("outcome", "done")
        filters = {k: v for k in ("department", "work_state") if (v := state.get(k))}
        return {
            "type": "payroll_flow_card", "flow_id": flow_id, "stage": stage,
            "month": state["month"], "label": PayrollPeriod.parse(state["month"]).label,
            "scope": " · ".join(filters.values()) or "All employees",
            "started_by": state.get("started_by"), "prechecks": state.get("prechecks"),
            "blockers": state.get("blockers", []), "batch_id": state.get("batch_id"),
            "run": state.get("run"), "validation": state.get("validation"),
            "totals": state.get("totals"), "steps": STAGES,
            "error": state.get("error") or (str(failed[0]) if failed else None),
        }  # fmt: skip

    def _audit(self, actor: Employee, flow_id: str, action: str, new: dict[str, Any]) -> None:
        with self.session_factory() as session:
            audit.record(session, action, actor=session.get(Employee, actor.id),
                         entity="payroll_flow", entity_id=flow_id, new=new,
                         request_id=flow_id)  # fmt: skip
            session.commit()


def sqlite_checkpointer(path: str) -> BaseCheckpointSaver:
    """A LangGraph checkpointer in its own SQLite file (``:memory:`` for tests)."""
    import sqlite3
    from pathlib import Path

    from langgraph.checkpoint.sqlite import SqliteSaver

    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(path, check_same_thread=False))
    saver.setup()
    return saver
