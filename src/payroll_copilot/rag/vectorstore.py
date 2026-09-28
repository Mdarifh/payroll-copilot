"""Chroma vector store (persistent, local folder) for the payroll knowledge base."""

from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

COLLECTION = "payroll_kb"


class KnowledgeStore:
    """Thin wrapper so the rest of the code never touches Chroma internals."""

    def __init__(self, embeddings: Embeddings, persist_dir: Path | None = None) -> None:
        kwargs: dict[str, Any] = {
            "collection_name": COLLECTION,
            "embedding_function": embeddings,
            "collection_metadata": {"hnsw:space": "cosine"},
        }
        if persist_dir is not None:
            persist_dir.mkdir(parents=True, exist_ok=True)
            kwargs["persist_directory"] = str(persist_dir)
        self.store = Chroma(**kwargs)

    def replace_document(self, document_name: str, chunks: list[Document]) -> None:
        """Idempotent upsert: remove old chunks of the document, add the new ones."""
        self.store.delete(where={"document_name": document_name})
        if chunks:
            self.store.add_documents(chunks, ids=[c.metadata["chunk_id"] for c in chunks])

    def clear(self) -> None:
        self.store.reset_collection()

    def all_chunks(self, where: dict | None = None) -> list[Document]:
        data = self.store.get(where=where, include=["documents", "metadatas"])
        return [
            Document(page_content=text, metadata=meta)
            for text, meta in zip(data["documents"], data["metadatas"], strict=True)
        ]

    def similarity(self, query: str, k: int, where: dict | None) -> list[tuple[Document, float]]:
        """Nearest chunks first (cosine distance; only the rank is used downstream)."""
        return self.store.similarity_search_with_score(query, k=k, filter=where)

    def count(self) -> int:
        return len(self.store.get(include=[])["ids"])
