"""Load and clean knowledge-base documents (Markdown, TXT, PDF, DOCX)."""

import re
from pathlib import Path

from langchain_core.documents import Document

from payroll_copilot.rag.metadata import KnowledgeBaseError

SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}
_PAGE_NUMBER = re.compile(r"^\s*(page\s+)?\d+(\s*(of|/)\s*\d+)?\s*$", re.IGNORECASE)


def clean_text(text: str) -> str:
    """Normalise whitespace and drop bare page-number lines."""
    text = text.replace("\r\n", "\n").replace(" ", " ")
    lines = [line.rstrip() for line in text.split("\n") if not _PAGE_NUMBER.match(line)]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def load_document(path: Path) -> list[Document]:
    """Return one Document per page (PDF) or one per file (other formats)."""
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        pages: list[tuple[str, int | None]] = [(path.read_text(encoding="utf-8"), None)]
    elif suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "", i) for i, page in enumerate(reader.pages)]
    elif suffix == ".docx":
        import docx2txt

        pages = [(docx2txt.process(str(path)) or "", None)]
    else:
        raise KnowledgeBaseError(f"Unsupported document type: {path.name}")
    result = []
    for raw, page in pages:
        text = clean_text(raw)
        if text:
            metadata = {"page": page} if page is not None else {}
            result.append(Document(page_content=text, metadata=metadata))
    if not result:
        raise KnowledgeBaseError(f"{path.name}: no text extracted")
    return result


def discover_documents(kb_root: Path) -> list[Path]:
    return sorted(
        p for p in kb_root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        and not p.name.lower().startswith("readme")
    )  # fmt: skip
