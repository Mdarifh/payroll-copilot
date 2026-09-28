"""Ingest + period-aware retrieval + answer chain (fake embeddings, fake LLM)."""

import pytest
from langchain_core.language_models import FakeListChatModel

from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.rag.chain import NOT_FOUND, answer_question, ungrounded_numbers
from payroll_copilot.rag.metadata import KnowledgeBaseError
from payroll_copilot.rag.retriever import build_filter

SEP, AUG = PayrollPeriod(2026, 9), PayrollPeriod(2026, 8)


def test_ingest_counts(kb):
    assert kb.store.count() > 100
    before = kb.store.count()
    report = kb.ingest()  # re-ingest without rebuild must not duplicate
    assert kb.store.count() == before
    assert report.per_module["pt"] == 28 and report.per_module["pf"] == 2


def test_period_selects_the_rule_version(kb):
    sep = kb.search("PF wage ceiling", module="pf", period=SEP, k=8)
    aug = kb.search("PF wage ceiling", module="pf", period=AUG, k=8)
    assert {c.metadata["document_name"] for c in sep} == {"EPF v2"}
    assert {c.metadata["document_name"] for c in aug} == {"EPF v1"}


def test_nothing_effective_means_nothing_returned(kb):
    assert kb.search("tax slabs", module="tds", period=PayrollPeriod(2027, 5)) == []
    answer = kb.ask("tax slabs", module="tds", period=PayrollPeriod(2027, 5))
    assert answer.answer == NOT_FOUND and not answer.found


def test_state_filter_for_pt(kb):
    chunks = kb.search("professional tax slab", module="pt", state="KA", k=10)
    assert {c.metadata["jurisdiction"] for c in chunks} <= {"IN", "IN-KA"}
    assert chunks and all(c.metadata["rule_name"] == "PT-KA" for c in chunks)


def test_policy_documents_are_searchable(kb):
    chunks = kb.search("LOP reversal", module="attendance", k=3)
    assert chunks[0].metadata["doc_type"] == "policy"
    assert "LOP reversal" in " ".join(c.text for c in chunks)


def test_citation_format(kb):
    chunk = kb.search("PF wage ceiling", module="pf", period=SEP, k=1)[0]
    assert chunk.citation.startswith("EPF v2")
    assert "2026-09-01 to open, IN, UNVERIFIED" in chunk.citation


def test_build_filter():
    f = build_filter("pt", SEP, "MH")
    assert {"module": "pt"} in f["$and"]
    assert {"effective_from_int": {"$lte": 20260930}} in f["$and"]
    assert {"jurisdiction": {"$in": ["IN", "IN-MH"]}} in f["$and"]
    assert build_filter(None, None, None) is None
    assert build_filter("pf", None, None) == {"module": "pf"}


def test_missing_kb_folder(kb, tmp_path):
    kb.kb_root = tmp_path / "missing"
    with pytest.raises(KnowledgeBaseError, match="not found"):
        kb.ingest()


class TestChain:
    def chunks(self, kb):
        return kb.search("PF wage ceiling", module="pf", period=SEP, k=2)

    def test_extractive_without_llm(self, kb):
        answer = answer_question("PF ceiling?", self.chunks(kb))
        assert not answer.used_llm and answer.found
        assert answer.answer.startswith("Relevant excerpts (LLM disabled)")
        assert answer.sources[0].startswith("[1] EPF v2")

    def test_llm_answer_grounded(self, kb):
        llm = FakeListChatModel(responses=["From September 2026 the PF wage ceiling is ₹25000 [1]."])
        answer = answer_question("PF ceiling?", self.chunks(kb), llm)
        assert answer.used_llm and answer.found and answer.warnings == []

    def test_llm_answer_with_invented_number_is_flagged(self, kb):
        llm = FakeListChatModel(responses=["The PF wage ceiling is ₹30000 [1]."])
        answer = answer_question("PF ceiling?", self.chunks(kb), llm)
        assert answer.warnings == ["Answer contains numbers not present in the sources: ['30000']"]

    def test_llm_not_found(self, kb):
        llm = FakeListChatModel(responses=[NOT_FOUND])
        assert not answer_question("gratuity?", self.chunks(kb), llm).found

    def test_ungrounded_numbers_ignores_commas_and_citations(self):
        assert ungrounded_numbers("₹25,000 as per [2]", "ceiling 25000") == []


@pytest.mark.slow
def test_real_local_model_ranks_the_ceiling_section(tmp_path, kb_copy, make_settings):
    """Uses the real bge-small model only if it is already cached locally."""
    if not (PROJECT_ROOT / "data" / "models").exists():
        pytest.skip("local embedding model not downloaded")
    from payroll_copilot.rag.service import KnowledgeBase

    kb = KnowledgeBase(settings=make_settings(), persist_dir=tmp_path / "chroma", kb_root=kb_copy)
    kb.ingest(rebuild=True)
    top = kb.search("What is the PF wage ceiling?", module="pf", period=SEP, k=1)[0]
    assert top.metadata["section"].endswith("Wage ceilings")
    assert "₹25000" in top.text
