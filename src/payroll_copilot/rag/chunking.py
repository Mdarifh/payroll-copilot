"""Split documents into retrieval chunks, keeping section headings as metadata."""

import hashlib

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from payroll_copilot.rag.metadata import DocumentMetadata

CHUNK_SIZE = 900
CHUNK_OVERLAP = 120
_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]


def chunk_documents(
    pages: list[Document], meta: DocumentMetadata, *, markdown: bool
) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    header_splitter = MarkdownHeaderTextSplitter(_HEADERS, strip_headers=False)
    base = meta.to_chroma()
    chunks: list[Document] = []
    for page in pages:
        sections = header_splitter.split_text(page.page_content) if markdown else [page]
        for section in sections:
            heading = " > ".join(
                section.metadata[k] for _, k in _HEADERS if section.metadata.get(k)
            )
            for piece in splitter.split_text(section.page_content):
                index = len(chunks)
                digest = hashlib.sha1(
                    f"{meta.document_name}|{index}|{piece}".encode()
                ).hexdigest()[:16]
                chunks.append(
                    Document(
                        page_content=piece,
                        metadata={
                            **base,
                            "section": heading,
                            "page": page.metadata.get("page", -1),
                            "chunk_index": index,
                            "chunk_id": f"{meta.document_name}::{index}::{digest}",
                        },
                    )
                )
    return chunks
