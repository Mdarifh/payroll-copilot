"""Ingest knowledge_base/ into the vector store."""

from dataclasses import dataclass, field
from pathlib import Path

from payroll_copilot.rag.chunking import chunk_documents
from payroll_copilot.rag.loaders import discover_documents, load_document
from payroll_copilot.rag.metadata import KnowledgeBaseError, load_metadata
from payroll_copilot.rag.vectorstore import KnowledgeStore


@dataclass
class IngestReport:
    documents: int = 0
    chunks: int = 0
    per_module: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def ingest_knowledge_base(
    kb_root: Path, store: KnowledgeStore, *, rebuild: bool = False, strict: bool = True
) -> IngestReport:
    if not kb_root.is_dir():
        raise KnowledgeBaseError(f"Knowledge base folder not found: {kb_root}")
    if rebuild:
        store.clear()
    report = IngestReport()
    for path in discover_documents(kb_root):
        try:
            meta = load_metadata(path, kb_root)
            chunks = chunk_documents(load_document(path), meta, markdown=path.suffix == ".md")
        except KnowledgeBaseError as exc:
            if strict:
                raise
            report.errors.append(str(exc))
            continue
        store.replace_document(meta.document_name, chunks)
        report.documents += 1
        report.chunks += len(chunks)
        report.per_module[meta.module] = report.per_module.get(meta.module, 0) + 1
    return report
