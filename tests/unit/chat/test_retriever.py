"""LangChain RAG: the PolicyRetriever (keyword + semantic search fused), embeddings."""

from datetime import date

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from sqlalchemy import select

from payroll_copilot.db.models import KnowledgeChunk
from payroll_copilot.services import knowledge as kb
from payroll_copilot.services import policy_retriever as pr
from tests.unit.chat.test_knowledge import emp, kb_root  # noqa: F401 - fixture

ON = date(2026, 10, 3)
# a tiny "meaning" space: words that mean the same thing share a dimension
CONCEPTS = [{"leave", "vacation", "holiday", "off", "absence"},
            {"approve", "approves", "approval", "sanction", "permission", "allowed"},
            {"payroll", "salary", "run", "steps", "process"},
            {"pf", "provident", "ceiling", "wage"}]  # fmt: skip


class ConceptEmbeddings(Embeddings):
    model = "concept-test"

    def __init__(self) -> None:
        self.queries = 0

    def _vector(self, text: str) -> list[float]:
        words = set(kb.tokens(text)) | set(text.lower().split())
        return [float(len(words & c)) for c in CONCEPTS] + [0.01]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries += 1
        return self._vector(text)


class DownEmbeddings(ConceptEmbeddings):
    def embed_query(self, text: str) -> list[float]:
        raise ConnectionError("Failed to connect to Ollama")


@pytest.fixture
def kb_session(seeded_session, kb_root):  # noqa: F811
    kb.ingest(seeded_session, kb_root)
    kb.embed_chunks(seeded_session, ConceptEmbeddings())
    return seeded_session


def retriever(s, code, embeddings=None, **kw) -> pr.PolicyRetriever:
    return pr.PolicyRetriever(session=s, user=emp(s, code), on=ON, embeddings=embeddings,
                              min_similarity=0.5, **kw)  # fmt: skip


def test_returns_langchain_documents_with_sources(kb_session):
    docs = retriever(kb_session, "EMP-101").invoke("who approves leave")
    assert docs and all(isinstance(d, Document) for d in docs)
    top = docs[0]
    assert top.metadata["title"] == "Leave policy" and top.metadata["source"] == "Test handbook"
    assert top.page_content == top.metadata["text"] and top.metadata["matched_by"] == ["keyword"]


def test_meaning_finds_a_paraphrase_keyword_search_misses(kb_session):
    s, question = kb_session, "is a vacation sanctioned by someone"
    assert kb.search(s, emp(s, "EMP-101"), question, on=ON) == []
    assert retriever(s, "EMP-101").invoke(question) == []  # keyword only: nothing
    docs = retriever(s, "EMP-101", ConceptEmbeddings()).invoke(question)
    assert docs[0].metadata["title"] == "Leave policy"
    assert docs[0].metadata["matched_by"] == ["meaning"]


def test_both_searches_agreeing_rank_first(kb_session):
    docs = retriever(kb_session, "EMP-101", ConceptEmbeddings()).invoke("leave approval")
    assert docs[0].metadata["matched_by"] == ["keyword", "meaning"]


def test_semantic_search_keeps_role_and_date_filters(kb_session):
    s = kb_session
    steps = "salary process steps"
    employee = retriever(s, "EMP-101", ConceptEmbeddings()).invoke(steps)
    assert all(d.metadata["title"] != "Payroll SOP" for d in employee)  # HR-only document
    hr = retriever(s, "HR-001", ConceptEmbeddings()).invoke(steps)
    assert hr[0].metadata["title"] == "Payroll SOP"
    pf = retriever(s, "EMP-101", ConceptEmbeddings()).invoke("provident wage ceiling")
    assert [d.metadata["title"] for d in pf if d.metadata["policy_type"] == "pf"] == ["EPF v2"]


def test_unrelated_question_finds_nothing(kb_session):
    assert retriever(kb_session, "EMP-101", ConceptEmbeddings()).invoke("cricket score") == []


def test_embedding_model_down_falls_back_to_keywords(kb_session):
    docs = retriever(kb_session, "EMP-101", DownEmbeddings()).invoke("who approves leave")
    assert docs[0].metadata["title"] == "Leave policy"


def test_chunks_are_embedded_once_per_model(kb_session):
    s = kb_session
    chunks = list(s.scalars(select(KnowledgeChunk)))
    assert chunks and all(c.embedding_model == "concept-test" and c.embedding for c in chunks)
    assert kb.embed_chunks(s, ConceptEmbeddings()) == 0  # nothing new

    class Other(ConceptEmbeddings):
        model = "other-model"

    assert kb.embed_chunks(s, Other()) == len(chunks)  # a new model re-embeds everything


def test_ingest_embeds_when_configured(seeded_session, kb_root, monkeypatch):  # noqa: F811
    monkeypatch.setattr(pr, "get_embeddings", lambda settings=None: ConceptEmbeddings())
    assert kb.ingest(seeded_session, kb_root)["embedded"] > 0
    (kb_root / "leave/leave.md").write_text("# Leave\n\nCasual leave: 10 days.\n", encoding="utf-8")

    class Broken(ConceptEmbeddings):
        def embed_documents(self, texts):
            raise ConnectionError("Failed to connect to Ollama")

    monkeypatch.setattr(pr, "get_embeddings", lambda settings=None: Broken())
    result = kb.ingest(seeded_session, kb_root)
    assert result["updated"] == 1 and "embeddings not updated" in result["errors"][-1]


def test_semantic_search_is_off_by_default(make_settings):
    assert pr.get_embeddings(make_settings()) is None
    on = pr.get_embeddings(make_settings(kb_embedding_model="nomic-embed-text"))
    assert on.model == "nomic-embed-text"


def test_fuse_orders_by_reciprocal_rank():
    a, b, c = ({"chunk_id": i} for i in (1, 2, 3))
    fused = pr.fuse([a, b], [b, c])
    assert [p["chunk_id"] for p in fused] == [2, 1, 3]
    assert fused[0]["matched_by"] == ["keyword", "meaning"]
