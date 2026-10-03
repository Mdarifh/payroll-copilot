"""Employee Directory endpoints. Role scoping happens in the service (server-side)."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import current_user, get_session, require_hr
from payroll_copilot.core.dates import parse_month
from payroll_copilot.db.models import Employee
from payroll_copilot.services import employee_directory as svc
from payroll_copilot.services import salary_structures as structures

router = APIRouter(tags=["employee directory"])


@router.get("/employees", summary="Search employees (paged, filtered)")
def employees(search: str | None = Query(None, max_length=100),
              department: str | None = Query(None, max_length=100),
              location: str | None = Query(None, max_length=100),
              structure: str | None = Query(None, max_length=32),
              page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
              user: Employee = Depends(current_user),
              session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.list_employees(session, user, search, department, location, structure, page,
                              page_size)  # fmt: skip


@router.get("/employees/filters", summary="Departments, locations and structures to filter by")
def filters(user: Employee = Depends(current_user),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.filters(session, user)


@router.get("/employees/{code}/salary", summary="Salary structure of an employee for a month")
def salary(code: str, month: str | None = Query(None, max_length=30),
           user: Employee = Depends(current_user),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.salary_details(session, user, code, parse_month(month) if month else None)


@router.get("/employees/{code}/attendance", summary="Attendance of an employee for a month")
def attendance(code: str, month: str | None = Query(None, max_length=30),
               user: Employee = Depends(current_user),
               session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.attendance_details(session, user, code, parse_month(month) if month else None)


@router.get("/employees/{code}/overtime", summary="Overtime and one-off pay of a month")
def overtime(code: str, month: str | None = Query(None, max_length=30),
             user: Employee = Depends(current_user),
             session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.pay_extras(session, user, code, parse_month(month) if month else None)


@router.get("/structures", summary="Salary structure templates and how many use each")
def structures_list(_: Employee = Depends(require_hr),
                    session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return structures.list_structures(session)


@router.get("/structures/{code}", summary="Salary structure template (components and basis)")
def structure(code: str, _: Employee = Depends(current_user),
              session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return structures.structure_detail(session, code)


@router.get("/structures/{code}/employees", summary="Employees currently on a structure")
def structure_employees(code: str, page: int = Query(1, ge=1),
                        page_size: int = Query(20, ge=1, le=100),
                        _: Employee = Depends(require_hr),
                        session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return structures.structure_employees(session, code, page, page_size)


@router.get("/structures/{code}/preview", summary="Breakup of a monthly gross (payroll engine)")
def structure_preview(code: str, gross: str = Query(..., max_length=20),
                      _: Employee = Depends(require_hr)) -> dict[str, Any]:  # fmt: skip
    return structures.preview(code, gross)
