"""Phase 9: knowledge base (RAG) - ingest, search with roles / dates, uploads, chat."""

from datetime import date

import pytest
from sqlalchemy import select

from payroll_copilot.db.models import Employee, KnowledgeDocument
from payroll_copilot.services import knowledge as kb

META = """module: {module}
document_name: {name}
effective_from: {start}
effective_to: {end}
jurisdiction: IN
source: Test handbook
status: UNVERIFIED
doc_type: policy
access_roles: {roles}
"""


def write(root, rel, text, name, module="leave", roles="[employee, manager, hr]",
          start="2025-04-01", end="null"):  # fmt: skip
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.with_name(path.name + ".meta.yaml").write_text(
        META.format(module=module, name=name, roles=roles, start=start, end=end), encoding="utf-8")


@pytest.fixture
def kb_root(tmp_path):
    write(tmp_path, "leave/leave.md", "# Leave\n\n## Entitlement\n\n- Casual leave: 12 days a year.\n"
          "- Sick leave: 12 days a year.\n\n## Approval\n\nThe manager approves leave.\n", "Leave policy")
    write(tmp_path, "payroll/sop.md", "# SOP\n\n## Steps\n\nRun pre-checks, review LOP, then run "
          "payroll and validate.\n", "Payroll SOP", module="payroll", roles="[hr]")
    write(tmp_path, "pf/old.md", "# PF old\n\n## Ceiling\n\nPF wage ceiling 15000.\n", "EPF v1",
          module="pf", start="2014-09-01", end="2026-09-16")
    write(tmp_path, "pf/new.md", "# PF new\n\n## Ceiling\n\nPF wage ceiling 25000.\n", "EPF v2",
          module="pf", start="2026-09-17")
    (tmp_path / "broken.md").write_text("no metadata here", encoding="utf-8")
    return tmp_path


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def test_ingest_is_incremental(seeded_session, kb_root):
    s = seeded_session
    first = kb.ingest(s, kb_root)
    assert first["added"] == 4 and any("broken.md" in e for e in first["errors"])
    assert kb.ingest(s, kb_root)["unchanged"] == 4
    (kb_root / "leave/leave.md").write_text("# Leave\n\n## Entitlement\n\nCasual leave: 10 days.\n",
                                            encoding="utf-8")  # fmt: skip
    (kb_root / "pf/old.md").unlink()
    again = kb.ingest(s, kb_root)
    assert (again["updated"], again["removed"]) == (1, 1)


def test_search_hinglish_roles_and_dates(seeded_session, kb_root):
    s = seeded_session
    kb.ingest(s, kb_root)
    employee, hr = emp(s, "EMP-101"), emp(s, "HR-001")
    a = kb.answer(s, employee, "kitni chhutti milti hai?")
    assert a["found"] and a["passages"][0]["title"] == "Leave policy"
    assert "12 days" in a["text"]
    assert not kb.answer(s, employee, "payroll steps sop")["found"]  # HR-only document
    assert kb.answer(s, hr, "payroll steps sop")["passages"][0]["title"] == "Payroll SOP"
    old = kb.search(s, employee, "PF wage ceiling", on=date(2026, 8, 1))
    new = kb.search(s, employee, "PF wage ceiling", on=date(2026, 10, 1))
    assert [p["title"] for p in old] == ["EPF v1"] and [p["title"] for p in new] == ["EPF v2"]
    assert not kb.answer(s, employee, "what is the meaning of life")["found"]


def test_upload_rejects_personal_data_and_can_be_removed(seeded_session, kb_root):
    s = seeded_session
    hr = emp(s, "HR-001")
    kb.ingest(s, kb_root)
    with pytest.raises(kb.KnowledgeError, match="PAN"):
        kb.upload(s, hr, "x.md", "Employee ABCDE1234F gets a bonus", "Bonus", "payroll",
                  "policy", "HR mail", ["hr"], None, root=kb_root)  # fmt: skip
    with pytest.raises(kb.KnowledgeError, match=".md and .txt"):
        kb.upload(s, hr, "x.pdf", "text", "Bonus", "payroll", "policy", "HR mail", ["hr"], None,
                  root=kb_root)  # fmt: skip
    doc = kb.upload(s, hr, "reimb.md", "# Reimbursement\n\nFuel is reimbursed against bills.",
                    "Reimbursement policy", "reimbursement", "policy", "Handbook 4.2",
                    ["employee", "hr"], None, root=kb_root)  # fmt: skip
    assert doc["uploaded"] and doc["status"] == "UNVERIFIED"
    assert kb.answer(s, emp(s, "EMP-101"), "fuel reimbursement bills")["found"]
    kb.remove_upload(s, hr, doc["id"], root=kb_root)
    assert s.get(KnowledgeDocument, doc["id"]) is None
    with pytest.raises(kb.KnowledgeError, match="Only uploaded"):
        kb.remove_upload(s, hr, s.scalar(select(KnowledgeDocument.id)), root=kb_root)


def test_chat_answers_policy_questions_with_sources(seeded_session, kb_root):
    from payroll_copilot.chat.agent import ChatAgent
    from payroll_copilot.chat.tools import ToolContext
    from tests.unit.chat.conftest import FakeChatModel

    s = seeded_session
    kb.ingest(s, kb_root)
    ctx = ToolContext(session=s, user=emp(s, "EMP-101"), today=date(2026, 10, 3))
    reply = ChatAgent(FakeChatModel()).ask(ctx, "Leave approval kaun karta hai?", "c")
    assert reply.tools_used[0] == "router:knowledge_question"
    card = reply.messages[1]
    assert card["type"] == "knowledge_card" and card["passages"][0]["source"] == "Test handbook"
