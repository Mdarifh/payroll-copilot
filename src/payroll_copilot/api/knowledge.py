"""Knowledge base endpoints. Everyone can search what their role may read; HR manages it."""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import current_user, get_session, require_admin
from payroll_copilot.db.models import Employee
from payroll_copilot.services import knowledge as svc

router = APIRouter(prefix="/knowledge", tags=["knowledge base"])


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=4, max_length=120)
    content: str = Field(min_length=1, max_length=svc.MAX_UPLOAD)
    title: str = Field(min_length=3, max_length=200)
    policy_type: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z_]+$")
    document_type: str = Field(default="policy", pattern=r"^(policy|sop|rule|faq)$")
    source: str = Field(min_length=3, max_length=500)
    access_roles: list[str] = Field(default_factory=lambda: list(svc.ALL_ROLES))
    effective_from: date | None = None


class RemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


@router.get("/search", summary="Search policies and rules (filtered by your role)")
def search(q: str = Query(min_length=2, max_length=300),
           policy_type: str | None = Query(None, max_length=32),
           user: Employee = Depends(current_user),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return {"query": q, "results": svc.search(session, user, q, policy_type, date.today(), 8),
            "answer": svc.answer(session, user, q)}  # fmt: skip


@router.get("/documents", summary="Documents you may read")
def documents(user: Employee = Depends(current_user),
              session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return {"documents": svc.list_documents(session, user), "stats": svc.stats(session)}


@router.get("/documents/{doc_id}", summary="One document with its text")
def document(doc_id: int, user: Employee = Depends(current_user),
             session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.document(session, user, doc_id)


@router.post("/documents", status_code=201, summary="Upload a policy (.md/.txt, no personal data)")
def upload(body: UploadRequest, hr: Employee = Depends(require_admin),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.upload(session, hr, body.filename, body.content, body.title, body.policy_type,
                      body.document_type, body.source, body.access_roles, body.effective_from)


@router.post("/documents/{doc_id}/remove", summary="Remove an uploaded document")
def remove(doc_id: int, body: RemoveRequest, hr: Employee = Depends(require_admin),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    svc.remove_upload(session, hr, doc_id)
    return {"removed": doc_id, "reason": body.reason}


@router.post("/reindex", summary="Re-index knowledge_base/ now")
def reindex(_: Employee = Depends(require_admin),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.ingest(session)
