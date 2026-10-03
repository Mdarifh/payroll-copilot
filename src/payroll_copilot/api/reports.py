"""Reports (payroll staff): on screen as JSON, or downloaded as CSV / Excel / PDF."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import get_session, require_hr
from payroll_copilot.core.dates import parse_month
from payroll_copilot.db.models import Employee
from payroll_copilot.services import audit
from payroll_copilot.services import reports as svc

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", summary="Available reports")
def available(_: Employee = Depends(require_hr)) -> list[dict[str, str]]:
    return [{"key": k, "title": title} for k, (title, _) in svc.REPORTS.items()]


@router.get("/{key}", summary="One report for a month (format: json | csv | xlsx | pdf)",
            response_model=None)  # fmt: skip
def report(key: str, month: str = Query(max_length=30),
           department: str | None = Query(None, max_length=100),
           format: str = Query("json", pattern="^(json|csv|xlsx|pdf)$"),
           user: Employee = Depends(require_hr),
           session: Session = Depends(get_session)) -> Any:  # fmt: skip
    built = svc.build(session, key, parse_month(month), department)
    if format == "json":
        return built.as_dict()
    content, media, filename = svc.export(built, format)
    audit.record(session, "report.download", actor=user, entity="report", entity_id=key,
                 new={"period": built.period, "format": format, "rows": len(built.rows)})
    return Response(content, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
