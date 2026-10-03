"""Background worker for bulk payroll (a database-backed job queue, no Redis needed).

* Picks the oldest QUEUED/RUNNING batch and processes its PENDING items in chunks.
* Each employee runs in its own transaction: payroll run + item status + audit commit
  together, or not at all. So a crash or restart never processes anyone twice; on start
  the worker simply continues RUNNING batches where they stopped.
* One employee's error is recorded on its item (FAILED + message) and the batch goes on.
* Temporary errors (locked database, timeouts) are retried with backoff; each attempt
  runs in a savepoint so a failed attempt leaves nothing behind.
* Already processed employee-months are SKIPPED (idempotent).
"""

import logging
import threading
from collections.abc import Callable
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import BatchItem, Employee, PayrollBatch
from payroll_copilot.domain.enums import BatchItemStatus, BatchStatus
from payroll_copilot.repositories.payroll_repo import PayrollRepository
from payroll_copilot.services.batch_service import now
from payroll_copilot.services.reliability import with_retry

log = logging.getLogger(__name__)

# (session, employee, period, run_by, batch_id) -> net pay
Processor = Callable[[Session, Employee, PayrollPeriod, Employee, str], Decimal]


def default_processor(session: Session, employee: Employee, period: PayrollPeriod,
                      run_by: Employee, batch_id: str) -> Decimal:  # fmt: skip
    from payroll_copilot.services.payslip import process_payroll

    return process_payroll(session, employee, period, run_by, batch_id=batch_id).net_pay


class BatchWorker:
    def __init__(
        self, session_factory: sessionmaker[Session], processor: Processor = default_processor,
        chunk_size: int | None = None, max_attempts: int | None = None,
        poll_seconds: float = 1.0, retry_delay: float = 0.5,
    ) -> None:  # fmt: skip
        settings = get_settings()
        self.sessions = session_factory
        self.processor = processor
        self.chunk_size = chunk_size or settings.batch_chunk_size
        self.max_attempts = max_attempts or settings.batch_max_attempts
        self.poll_seconds = poll_seconds
        self.retry_delay = retry_delay
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="batch-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self.run_once()
            except Exception:  # noqa: BLE001 - the worker must never die
                log.exception("batch worker step failed")
                busy = 0
            if not busy:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    # ------------------------------------------------------------------ work

    def run_until_idle(self, max_items: int | None = None) -> int:
        """Process until nothing is queued (or ``max_items`` were handled). For tests/CLI."""
        handled = 0
        while max_items is None or handled < max_items:
            step = self.run_once(None if max_items is None else max_items - handled)
            if step == 0:
                break
            handled += step
        return handled

    def run_once(self, limit: int | None = None) -> int:
        """One chunk of the oldest active batch. Returns how many items were handled."""
        with self.sessions() as session:
            batch = session.scalar(
                select(PayrollBatch)
                .where(PayrollBatch.status.in_([BatchStatus.QUEUED, BatchStatus.RUNNING]))
                .order_by(PayrollBatch.run_requested_at, PayrollBatch.id)
            )  # fmt: skip
            if batch is None:
                return 0
            if batch.status is BatchStatus.QUEUED:
                batch.status = BatchStatus.RUNNING
                batch.started_at = batch.started_at or now()
                session.commit()
            size = min(self.chunk_size, limit) if limit else self.chunk_size
            item_ids = session.scalars(
                select(BatchItem.id)
                .where(BatchItem.batch_pk == batch.id, BatchItem.status == BatchItemStatus.PENDING)
                .order_by(BatchItem.id).limit(size)
            ).all()  # fmt: skip
            batch_pk = batch.id
            if not item_ids:
                self._finish(session, batch)
                return 1  # state changed; look again for the next batch
        for item_id in item_ids:
            if self._stop.is_set():
                break
            self._process_item(batch_pk, item_id)
        return len(item_ids)

    def _finish(self, session: Session, batch: PayrollBatch) -> None:
        failed = session.scalar(select(func.count()).select_from(BatchItem).where(
            BatchItem.batch_pk == batch.id, BatchItem.status == BatchItemStatus.FAILED))
        batch.status = (BatchStatus.COMPLETED_WITH_ERRORS if failed
                        else BatchStatus.COMPLETED)  # fmt: skip
        batch.finished_at = now()
        session.commit()
        log.info("batch %s finished: %s (%d failed)", batch.batch_id, batch.status, failed)

    def _process_item(self, batch_pk: int, item_id: int) -> None:
        session = self.sessions()
        try:
            item = session.get(BatchItem, item_id)
            if item is None or item.status is not BatchItemStatus.PENDING:
                return
            batch = session.get(PayrollBatch, batch_pk)
            employee = session.get(Employee, item.employee_id)
            run_by = session.get(Employee, batch.created_by_id)
            period = PayrollPeriod.parse(batch.period)
            item.attempts += 1
            # a recalculation batch re-runs processed (unlocked) payslips on purpose
            recalculate = bool((batch.filters or {}).get("recalculate"))
            if not recalculate and PayrollRepository(session).get(employee.id, period) is not None:
                item.status = BatchItemStatus.SKIPPED
                item.error = "already processed for this month"
            else:
                def attempt() -> Decimal:
                    with session.begin_nested():  # a failed attempt leaves nothing behind
                        return self.processor(session, employee, period, run_by, batch.batch_id)

                item.net_pay = with_retry(attempt, attempts=self.max_attempts,
                                          base_delay=self.retry_delay)  # fmt: skip
                item.status = BatchItemStatus.DONE
            item.processed_at = now()
            session.commit()
        except Exception as exc:  # noqa: BLE001 - recorded per employee, batch continues
            session.rollback()
            self._mark_failed(session, item_id, exc)
        finally:
            session.close()

    def _mark_failed(self, session: Session, item_id: int, exc: Exception) -> None:
        try:
            item = session.get(BatchItem, item_id)
            item.status = BatchItemStatus.FAILED
            item.attempts += 1
            from payroll_copilot.core.exceptions import PayrollCopilotError

            item.error = (str(exc) if isinstance(exc, PayrollCopilotError)
                          else f"{type(exc).__name__}: {exc}")[:1000]  # fmt: skip
            item.processed_at = now()
            session.commit()
        except Exception:  # noqa: BLE001 - leave it PENDING; picked up again later
            session.rollback()
            log.exception("could not record failure of batch item %s", item_id)
