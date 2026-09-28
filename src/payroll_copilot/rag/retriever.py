"""Period-aware hybrid retrieval with metadata reranking.

1. Metadata filter: module, jurisdiction (IN + state), and effective dates that
   overlap the requested payroll period. Documents not effective in the period
   are never returned for that period.
2. Hybrid search: vector similarity + BM25 keyword match, fused with
   reciprocal-rank fusion (RRF).
3. Rerank: small boosts for query words in the section heading, the most
   specific jurisdiction, VERIFIED status and rule documents; ties broken by the
   latest effective_from (newest version).
"""

import re
from dataclasses import dataclass

from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.rag.metadata import date_int
from payroll_copilot.rag.vectorstore import KnowledgeStore

RRF_K = 60
_TOKEN = re.compile(r"[a-z0-9₹%.]+")


def _tokens(text: str) -> list[str]:
    """Lower-case tokens with a light plural strip (ceilings -> ceiling)."""
    return [t[:-1] if len(t) > 4 and t.endswith("s") else t for t in _TOKEN.findall(text.lower())]


@dataclass(frozen=True)
class RetrievedChunk:
    text: str
    metadata: dict
    score: float
    vector_rank: int | None
    keyword_rank: int | None

    @property
    def citation(self) -> str:
        m = self.metadata
        section = f" › {m['section']}" if m.get("section") else ""
        return (
            f"{m['document_name']}{section} (v{m['version']}, {m['effective_from']} to "
            f"{m['effective_to']}, {m['jurisdiction']}, {m['status']})"
        )


def build_filter(
    module: str | None, period: PayrollPeriod | None, state: str | None
) -> dict | None:
    clauses: list[dict] = []
    if module:
        clauses.append({"module": module})
    if period:
        clauses.append({"effective_from_int": {"$lte": date_int(period.end)}})
        clauses.append({"effective_to_int": {"$gte": date_int(period.start)}})
    if state:
        clauses.append({"jurisdiction": {"$in": ["IN", f"IN-{state}"]}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class PolicyRetriever:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def retrieve(
        self,
        query: str,
        *,
        module: str | None = None,
        period: PayrollPeriod | None = None,
        state: str | None = None,
        k: int = 4,
        candidates: int = 12,
    ) -> list[RetrievedChunk]:
        where = build_filter(module, period, state)
        pool = self.store.all_chunks(where)
        if not pool:
            return []

        vector_hits = self.store.similarity(query, k=min(candidates, len(pool)), where=where)
        vector_rank = {d.metadata["chunk_id"]: i for i, (d, _) in enumerate(vector_hits)}

        bm25 = BM25Okapi([_tokens(d.page_content + " " + d.metadata.get("section", ""))
                          for d in pool])  # fmt: skip
        scores = bm25.get_scores(_tokens(query))
        ordered = sorted(range(len(pool)), key=lambda i: scores[i], reverse=True)[:candidates]
        keyword_rank = {pool[i].metadata["chunk_id"]: r for r, i in enumerate(ordered)
                        if scores[i] > 0}  # fmt: skip

        by_id: dict[str, Document] = {d.metadata["chunk_id"]: d for d in pool}
        query_terms = {t for t in _tokens(query) if len(t) > 2}
        fused: list[RetrievedChunk] = []
        for chunk_id in set(vector_rank) | set(keyword_rank):
            doc = by_id[chunk_id]
            score = sum(1 / (RRF_K + 1 + rank[chunk_id])
                        for rank in (vector_rank, keyword_rank) if chunk_id in rank)  # fmt: skip
            score += self._boost(doc.metadata, state)
            # the last heading names the section; matching it is a strong topical signal
            heading = doc.metadata.get("section", "").split(">")[-1]
            score += 0.004 * len(query_terms & set(_tokens(heading)))
            fused.append(RetrievedChunk(
                text=doc.page_content, metadata=doc.metadata, score=score,
                vector_rank=vector_rank.get(chunk_id), keyword_rank=keyword_rank.get(chunk_id),
            ))  # fmt: skip
        fused.sort(key=lambda c: (c.score, c.metadata["effective_from_int"]), reverse=True)
        return fused[:k]

    @staticmethod
    def _boost(meta: dict, state: str | None) -> float:
        boost = 0.0
        if state and meta.get("jurisdiction") == f"IN-{state}":
            boost += 0.004  # state-specific beats national for state levies
        if meta.get("status") == "VERIFIED":
            boost += 0.002
        if meta.get("doc_type") == "rule":
            boost += 0.001
        return boost
