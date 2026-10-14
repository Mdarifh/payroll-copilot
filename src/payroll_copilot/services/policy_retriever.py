"""LangChain retriever over the policy knowledge base (the "R" of RAG).

``PolicyRetriever`` is a LangChain ``BaseRetriever``: it returns ``Document`` objects
whose metadata carries the source (title, section, status, effective dates). It merges
two searches with reciprocal rank fusion:

* keyword search (BM25, ``knowledge.search``) - always on;
* semantic search (Ollama embeddings, ``knowledge.semantic_search``) - when
  ``KB_EMBEDDING_MODEL`` is set; if the embedding model is down, keyword search alone
  answers.

Both apply the reader's role and the effective dates, so a passage the user may not
read is never retrieved. The knowledge base holds documents only, never employee data.
"""

import logging
from datetime import date
from functools import lru_cache
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from payroll_copilot.core.config import Settings, get_settings
from payroll_copilot.db.models import Employee
from payroll_copilot.services import knowledge

log = logging.getLogger(__name__)
RRF_K = 60  # reciprocal rank fusion constant (the usual value)


@lru_cache(maxsize=4)
def _ollama_embeddings(model: str, base_url: str) -> Embeddings:
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(model=model, base_url=base_url, client_kwargs={"timeout": 60.0})


def get_embeddings(settings: Settings | None = None) -> Embeddings | None:
    """The configured embedding model, or None when semantic search is off."""
    settings = settings or get_settings()
    if not settings.kb_embedding_model:
        return None
    return _ollama_embeddings(settings.kb_embedding_model, settings.ollama_base_url)


def fuse(*rankings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reciprocal rank fusion of passage lists (best first); ``matched_by`` says which
    searches found each passage."""
    scores: dict[int, float] = {}
    passages: dict[int, dict[str, Any]] = {}
    for name, ranking in zip(("keyword", "meaning"), rankings, strict=False):
        for rank, p in enumerate(ranking):
            cid = p["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            passage = passages.setdefault(cid, {**p, "matched_by": []})
            passage["matched_by"].append(name)
    return [passages[cid] for cid in sorted(scores, key=lambda c: -scores[c])]


class PolicyRetriever(BaseRetriever):
    """Policy passages the user may read, as LangChain Documents (best first)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: Any  # sqlalchemy Session of the current request
    user: Employee
    on: date | None = None  # only documents in force on this date
    k: int = 3
    policy_type: str | None = None
    embeddings: Embeddings | None = None
    min_similarity: float = 0.55

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        lexical = knowledge.search(self.session, self.user, query, self.policy_type, self.on,
                                   limit=self.k * 2)  # fmt: skip
        semantic: list[dict[str, Any]] = []
        if self.embeddings is not None:
            try:
                semantic = knowledge.semantic_search(
                    self.session, self.user, query, self.embeddings, self.policy_type, self.on,
                    limit=self.k * 2, min_similarity=self.min_similarity)  # fmt: skip
            except Exception:  # noqa: BLE001 - embedding model down: keyword search answers
                log.warning("semantic search unavailable, using keyword search", exc_info=True)
        return [Document(page_content=p["text"], metadata=p)
                for p in fuse(lexical, semantic)[: self.k]]  # fmt: skip


def retriever_for(session: Any, user: Employee, on: date | None = None, k: int = 3,
                  policy_type: str | None = None) -> PolicyRetriever:  # fmt: skip
    settings = get_settings()
    return PolicyRetriever(session=session, user=user, on=on, k=k, policy_type=policy_type,
                           embeddings=get_embeddings(settings),
                           min_similarity=settings.kb_min_similarity)  # fmt: skip


def retrieve(session: Any, user: Employee, query: str, on: date | None = None, k: int = 3,
             policy_type: str | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """Passages (dicts with their sources) for ``query`` - the retriever's Documents' metadata."""
    docs = retriever_for(session, user, on, k, policy_type).invoke(query)
    return [d.metadata for d in docs]
