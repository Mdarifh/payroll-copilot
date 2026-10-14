"""Knowledge base (RAG): ingest policy / rule documents, retrieve passages with sources.

Retrieval is BM25 over heading-aware chunks, computed in Python (a few hundred chunks:
instant, no extra packages, works on SQLite and PostgreSQL). Results are filtered by
tenant, the user's role (``access_roles`` in the document metadata) and effective dates.
The knowledge base never contains employee data: uploads with personal identifiers are
refused, and chat answers from it never include payroll records.
"""

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.core.exceptions import NotFoundError, PayrollCopilotError
from payroll_copilot.db.models import Employee, KnowledgeChunk, KnowledgeDocument
from payroll_copilot.services import audit

KB_ROOT = PROJECT_ROOT / "knowledge_base"
UPLOADS = "uploads"
ALL_ROLES = ["employee", "manager", "hr"]
CHUNK_CHARS = 900
MAX_UPLOAD = 200_000
TENANT = "default"

_STOP = frozenset({"a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are",
                    "be", "by", "with", "as", "at", "from", "that", "this", "it", "its",
                    "what", "which", "how", "why", "when", "who", "whom", "do", "does", "did",
                    "can", "could", "should", "would", "will", "shall", "may", "my", "me", "i",
                    "we", "our", "you", "your", "hai", "hain", "ka", "ki", "ke", "ko", "kya",
                    "kaise", "kyun", "kitna", "kitni", "kitne", "mein", "se", "aur",
                    "ya", "bhi", "batao", "bata", "samjhao", "explain", "tell", "please",
                    "about", "milta", "milti", "milte", "hota", "hoti", "hote"})  # fmt: skip
_SYNONYMS = {"chhutti": "leave", "chutti": "leave", "chhuttiyan": "leave", "leaves": "leave",
             "tankhwah": "salary", "tankha": "salary", "vetan": "salary", "ot": "overtime",
             "ghante": "hours", "ghanta": "hours", "niyam": "rule", "kaat": "deduct",
             "katauti": "deduction", "provident": "pf", "epf": "pf", "esi": "esic",
             "gratuity": "gratuity", "fnf": "fnf", "settlement": "fnf", "naukri": "employment",
             "process": "sop", "procedure": "sop", "steps": "sop"}  # fmt: skip


def reader_role(user: Employee) -> str:
    """Document audience of a user: payroll staff read what HR may read."""
    from payroll_copilot.auth.permissions import is_staff

    return "hr" if is_staff(user) else user.role.value


class KnowledgeError(PayrollCopilotError):
    """Invalid knowledge base document or upload."""


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        w = _SYNONYMS.get(w, w)
        if w in _STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]  # leaves -> leave, ceilings -> ceiling
        out.append(w)
    return out


# ---------------------------------------------------------------------- ingest


def _meta(path: Path) -> dict[str, Any]:
    side = path.with_name(path.name + ".meta.yaml")
    if not side.exists():
        raise KnowledgeError(f"{path.name}: missing {side.name} (metadata is never guessed)")
    data = yaml.safe_load(side.read_text(encoding="utf-8")) or {}
    for key in ("module", "document_name", "source", "status"):
        if not data.get(key):
            raise KnowledgeError(f"{side.name}: '{key}' is required")
    return data


def _date(value: Any) -> date | None:
    if value in (None, "", "null"):
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
_SECTIONS = MarkdownHeaderTextSplitter(_HEADERS, strip_headers=True)
_PIECES = RecursiveCharacterTextSplitter(chunk_size=CHUNK_CHARS, chunk_overlap=0,
                                         separators=["\n\n", "\n", " ", ""])  # fmt: skip


def chunk(title: str, text: str) -> list[tuple[str, str]]:
    """(heading path, text) pieces: LangChain splits on markdown headings, then by
    paragraph to at most CHUNK_CHARS each. Text before the first heading gets ``title``."""
    # boilerplate of generated rule pages, not policy content
    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("> Generated from"))
    out: list[tuple[str, str]] = []
    for section in _SECTIONS.split_text(text):
        path = [section.metadata[k] for _, k in _HEADERS if k in section.metadata]
        if "h1" not in section.metadata:
            path.insert(0, title)  # a "## Section" without a "# Title" sits under the title
        heading = " › ".join(path)
        out.extend((heading, piece.strip()) for piece in _PIECES.split_text(section.page_content)
                   if piece.strip())  # fmt: skip
    return out


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in (".md", ".txt")
                  and p.name.lower() != "readme.md")  # fmt: skip


def ingest(session: Session, root: Path = KB_ROOT, tenant: str = TENANT) -> dict[str, Any]:
    """Sync documents under ``root``: new / changed files are (re)chunked, deleted files
    removed. Unchanged files are skipped (content hash)."""
    seen: set[str] = set()
    stats = Counter()
    errors: list[str] = []
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        seen.add(rel)
        text = path.read_text(encoding="utf-8")
        try:
            meta = _meta(path)
        except KnowledgeError as exc:
            errors.append(str(exc))
            continue
        digest = hashlib.sha256((text + yaml.safe_dump(meta)).encode()).hexdigest()
        doc = session.scalar(select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant, KnowledgeDocument.path == rel))
        if doc is not None and doc.content_hash == digest:
            stats["unchanged"] += 1
            continue
        if doc is None:
            doc = KnowledgeDocument(tenant_id=tenant, path=rel)
            session.add(doc)
            stats["added"] += 1
        else:
            session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
            stats["updated"] += 1
        doc.title = str(meta["document_name"])[:200]
        doc.policy_type = str(meta["module"])[:32]
        doc.document_type = str(meta.get("doc_type", "policy"))[:32]
        doc.rule_name = meta.get("rule_name")
        doc.version = None if meta.get("version") is None else str(meta["version"])
        doc.jurisdiction = str(meta.get("jurisdiction", "IN"))
        doc.effective_from, doc.effective_to = _date(meta.get("effective_from")), _date(
            meta.get("effective_to"))
        doc.source, doc.status = str(meta["source"]), str(meta["status"]).upper()
        roles = meta.get("access_roles") or ALL_ROLES
        doc.access_roles = [r for r in roles if r in ALL_ROLES] or ["hr"]
        doc.content_hash, doc.meta = digest, {k: str(v) for k, v in meta.items()}
        session.flush()
        pieces = chunk(doc.title, text)
        session.add_all(KnowledgeChunk(document_id=doc.id, seq=i, heading=h[:300], text=t,
                                       created_at=datetime.now(UTC))
                        for i, (h, t) in enumerate(pieces))  # fmt: skip
        doc.chunk_count = len(pieces)
    gone = session.scalars(select(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == tenant, KnowledgeDocument.path.not_in(seen)))
    for doc in gone:
        session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == doc.id))
        session.delete(doc)
        stats["removed"] += 1
    session.flush()
    _INDEX.clear()
    result: dict[str, Any] = {k: stats.get(k, 0) for k in ("added", "updated", "unchanged",
                                                          "removed")}  # fmt: skip
    from payroll_copilot.services.policy_retriever import get_embeddings

    embeddings = get_embeddings()
    if embeddings is not None:
        try:
            result["embedded"] = embed_chunks(session, embeddings, tenant)
        except Exception as exc:  # noqa: BLE001 - keyword search still works without vectors
            errors.append(f"embeddings not updated: {exc}")
    return {**result, "errors": errors}


# ---------------------------------------------------------------------- search


@dataclass
class _Doc:
    chunk_id: int
    document_id: int
    terms: Counter
    length: int
    has_number: bool = False


_INDEX: dict[str, Any] = {}


def _index(session: Session, tenant: str) -> dict[str, Any]:
    signature = session.execute(
        select(func.count(KnowledgeChunk.id), func.max(KnowledgeDocument.updated_at))
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeDocument.tenant_id == tenant)).one()  # fmt: skip
    key = f"{tenant}:{signature[0]}:{signature[1]}"
    if _INDEX.get("key") == key:
        return _INDEX
    docs: list[_Doc] = []
    df: Counter = Counter()
    rows = session.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.document_id, KnowledgeChunk.heading,
               KnowledgeChunk.text, KnowledgeDocument.title)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeDocument.tenant_id == tenant))  # fmt: skip
    for cid, did, heading, text, title in rows:
        # heading and title count twice: a match there is a strong signal
        terms = Counter(tokens(f"{title} {heading} {title} {heading} {text}"))
        docs.append(_Doc(cid, did, terms, sum(terms.values()), bool(re.search(r"\d", text))))
        df.update(terms.keys())
    avg = sum(d.length for d in docs) / len(docs) if docs else 1
    _INDEX.clear()
    _INDEX.update(key=key, docs=docs, df=df, n=len(docs), avg=avg)
    return _INDEX


def _bm25(index: dict[str, Any], query: list[str], k1: float = 1.4, b: float = 0.75):
    n, avg, df = index["n"], index["avg"], index["df"]
    for d in index["docs"]:
        score = 0.0
        for t in set(query):
            tf = d.terms.get(t, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * d.length / avg))
        if score > 0:
            yield d, score


def _snippet(text: str, query: list[str], width: int = 320) -> str:
    """The best matching window of consecutive lines (reads as written)."""
    lines: list[str] = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        starts_item = re.match(r"^([-*|>]|\d+[.)]|#)", ln)
        if lines and not starts_item and raw.startswith((" ", "\t")) or (
                lines and not starts_item and not lines[-1].endswith((".", ":", "|"))):
            lines[-1] += " " + ln  # a wrapped bullet / sentence continues
        else:
            lines.append(ln)
    if not lines:
        return ""
    hits = [sum(t in tokens(ln) for t in query) for ln in lines]
    start = max(range(len(lines)), key=lambda i: (sum(hits[i:i + 3]), -i))
    out = " ".join(lines[start:start + 4])
    return out if len(out) <= width else out[: width - 1].rsplit(" ", 1)[0] + "…"


def search(session: Session, user: Employee, query: str, policy_type: str | None = None,
           on: date | None = None, limit: int = 5,
           tenant: str = TENANT) -> list[dict[str, Any]]:  # fmt: skip
    q = tokens(query)
    if not q:
        return []
    index = _index(session, tenant)
    # "how many / how much / kitni" questions want the passage that holds the number
    quantity = re.search(r"\b(kitn\w*|how (many|much)|amount|rate|limit|ceiling)\b", query.lower())
    scored = ((d, score * (1.5 if quantity and d.has_number else 1.0))
              for d, score in _bm25(index, q))
    ranked = sorted(scored, key=lambda x: -x[1])[: limit * 6]
    if not ranked:
        return []
    ids = {d.chunk_id for d, _ in ranked}
    rows = {c.id: (c, doc) for c, doc in session.execute(
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeChunk.id.in_(ids)))}  # fmt: skip
    role = reader_role(user)
    out, per_doc = [], Counter()
    needed = max(1, math.ceil(len(set(q)) / 2))
    for d, score in ranked:
        if sum(1 for t in set(q) if t in d.terms) < needed:
            continue  # a passage sharing only a common word is not an answer
        c, doc = rows[d.chunk_id]
        if not _readable(doc, role, policy_type, on) or per_doc[doc.id] >= 2:
            continue  # variety: at most 2 passages per document
        per_doc[doc.id] += 1
        out.append(_passage(c, doc, q, score))
        if len(out) >= limit:
            break
    return out


def _readable(doc: KnowledgeDocument, role: str, policy_type: str | None,
              on: date | None) -> bool:  # fmt: skip
    """May this reader see the document, and is it in force on ``on``?"""
    if role not in doc.access_roles or (policy_type and doc.policy_type != policy_type):
        return False
    return not (on and ((doc.effective_from and doc.effective_from > on)
                        or (doc.effective_to and doc.effective_to < on)))  # fmt: skip


def _passage(c: KnowledgeChunk, doc: KnowledgeDocument, q: list[str],
             score: float) -> dict[str, Any]:  # fmt: skip
    return {
        "chunk_id": c.id, "document_id": doc.id, "title": doc.title, "section": c.heading,
        "snippet": _snippet(c.text, q), "text": c.text, "score": round(score, 3),
        "source": doc.source, "status": doc.status, "jurisdiction": doc.jurisdiction,
        "policy_type": doc.policy_type, "document_type": doc.document_type,
        "effective_from": doc.effective_from and doc.effective_from.isoformat(),
        "effective_to": doc.effective_to and doc.effective_to.isoformat(),
    }  # fmt: skip


# ---------------------------------------------------------------------- embeddings


def _embedding_input(heading: str, text: str) -> str:
    return f"{heading}\n{text}"


def embed_chunks(session: Session, embeddings: Embeddings, tenant: str = TENANT) -> int:
    """Store a vector for every chunk that has none from this model; returns how many.

    Runs at ingest (API start, ``payroll-chatbot kb``, uploads) so searches only read.
    """
    model = getattr(embeddings, "model", None) or type(embeddings).__name__
    pending = list(session.scalars(
        select(KnowledgeChunk)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeDocument.tenant_id == tenant,
               (KnowledgeChunk.embedding_model.is_(None))
               | (KnowledgeChunk.embedding_model != model))))  # fmt: skip
    for start in range(0, len(pending), 32):
        batch = pending[start:start + 32]
        vectors = embeddings.embed_documents([_embedding_input(c.heading, c.text) for c in batch])
        for c, vector in zip(batch, vectors, strict=True):
            c.embedding, c.embedding_model = [round(float(x), 6) for x in vector], model
    session.flush()
    _VECTORS.clear()
    return len(pending)


_VECTORS: dict[str, Any] = {}


def _vectors(session: Session, model: str, tenant: str) -> list[tuple[int, list[float], float]]:
    """(chunk id, vector, norm) of every embedded chunk, cached until the KB changes."""
    signature = session.execute(
        select(func.count(KnowledgeChunk.id), func.max(KnowledgeDocument.updated_at))
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeDocument.tenant_id == tenant,
               KnowledgeChunk.embedding_model == model)).one()  # fmt: skip
    key = f"{tenant}:{model}:{signature[0]}:{signature[1]}"
    if _VECTORS.get("key") != key:
        rows = session.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.embedding)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(KnowledgeDocument.tenant_id == tenant,
                   KnowledgeChunk.embedding_model == model))  # fmt: skip
        _VECTORS.clear()
        _VECTORS.update(key=key, rows=[(cid, v, math.sqrt(sum(x * x for x in v)) or 1.0)
                                       for cid, v in rows if v])  # fmt: skip
    return _VECTORS["rows"]


def semantic_search(session: Session, user: Employee, query: str, embeddings: Embeddings,
                    policy_type: str | None = None, on: date | None = None, limit: int = 5,
                    min_similarity: float = 0.0,
                    tenant: str = TENANT) -> list[dict[str, Any]]:  # fmt: skip
    """Passages closest in meaning to ``query`` (cosine similarity of embeddings), with the
    same role / policy / date filters as keyword search. ``score`` is the similarity."""
    model = getattr(embeddings, "model", None) or type(embeddings).__name__
    rows = _vectors(session, model, tenant)
    if not rows or not query.strip():
        return []
    qv = embeddings.embed_query(query)
    qn = math.sqrt(sum(x * x for x in qv)) or 1.0
    scored = sorted(((cid, sum(a * b for a, b in zip(qv, v, strict=False)) / (qn * n))
                     for cid, v, n in rows), key=lambda x: -x[1])  # fmt: skip
    scored = [(cid, s) for cid, s in scored[: limit * 6] if s >= min_similarity]
    if not scored:
        return []
    found = {c.id: (c, doc) for c, doc in session.execute(
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .where(KnowledgeChunk.id.in_([cid for cid, _ in scored])))}  # fmt: skip
    role, q = reader_role(user), tokens(query)
    out, per_doc = [], Counter()
    for cid, similarity in scored:
        c, doc = found[cid]
        if not _readable(doc, role, policy_type, on) or per_doc[doc.id] >= 2:
            continue
        per_doc[doc.id] += 1
        out.append(_passage(c, doc, q, similarity))
        if len(out) >= limit:
            break
    return out


def answer(session: Session, user: Employee, question: str,
           on: date | None = None) -> dict[str, Any]:  # fmt: skip
    """Extractive answer: the best passages quoted with their sources (no invention).

    Passages come from the LangChain retriever (keyword + optional semantic search)."""
    from payroll_copilot.services.policy_retriever import retrieve

    passages = retrieve(session, user, question, on=on or date.today(), k=3)
    if not passages:
        return {"found": False, "passages": [],
                "text": "I couldn't find this in the knowledge base. Please ask HR, or add the "
                        "policy document in Knowledge Base."}  # fmt: skip
    top = passages[0]
    text = (f"From “{top['title']}” ({top['section'].split(' › ')[-1]}): {top['snippet']}"
            + (" Note: this document is marked UNVERIFIED." if top["status"] != "VERIFIED"
               else ""))  # fmt: skip
    return {"found": True, "passages": passages, "text": text}


# ---------------------------------------------------------------------- documents


def list_documents(session: Session, user: Employee, tenant: str = TENANT) -> list[dict[str, Any]]:
    docs = session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant)
                           .order_by(KnowledgeDocument.policy_type, KnowledgeDocument.title))
    return [_doc_dict(d) for d in docs if reader_role(user) in d.access_roles]


def _doc_dict(d: KnowledgeDocument) -> dict[str, Any]:
    return {"id": d.id, "title": d.title, "path": d.path, "policy_type": d.policy_type,
            "document_type": d.document_type, "version": d.version,
            "jurisdiction": d.jurisdiction, "status": d.status, "source": d.source,
            "effective_from": d.effective_from and d.effective_from.isoformat(),
            "effective_to": d.effective_to and d.effective_to.isoformat(),
            "access_roles": d.access_roles, "chunks": d.chunk_count,
            "uploaded": d.path.startswith(UPLOADS + "/"),
            "generated": "/generated/" in d.path}  # fmt: skip


def document(session: Session, user: Employee, doc_id: int, root: Path = KB_ROOT) -> dict[str, Any]:
    d = session.get(KnowledgeDocument, doc_id)
    if d is None or reader_role(user) not in d.access_roles:
        raise NotFoundError("Document not found")
    path = root / d.path
    return {**_doc_dict(d), "content": path.read_text(encoding="utf-8") if path.exists() else ""}


_PII = [(re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "a PAN number"),
        (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "an Aadhaar-like number"),
        (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "an email address"),
        (re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)"), "a phone number")]  # fmt: skip


def upload(session: Session, actor: Employee, filename: str, content: str, title: str,
           policy_type: str, document_type: str, source: str,
           access_roles: list[str], effective_from: date | None,
           root: Path = KB_ROOT) -> dict[str, Any]:  # fmt: skip
    if not filename.lower().endswith((".md", ".txt")):
        raise KnowledgeError("Only .md and .txt documents can be uploaded")
    if len(content.encode()) > MAX_UPLOAD:
        raise KnowledgeError("The document is larger than 200 KB")
    if not content.strip():
        raise KnowledgeError("The document is empty")
    for pattern, what in _PII:
        if pattern.search(content):
            raise KnowledgeError(f"The document seems to contain {what}. The knowledge base is "
                                 "for policies only - remove personal data and try again.")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "document"
    folder = root / UPLOADS
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug}.md"
    meta = {"module": policy_type.lower()[:32], "document_name": title[:200], "doc_type":
            document_type.lower()[:32], "version": "1", "jurisdiction": "IN",
            "effective_from": (effective_from or date.today()).isoformat(), "effective_to": None,
            "source": source[:500], "status": "UNVERIFIED",
            "access_roles": [r for r in access_roles if r in ALL_ROLES] or ["hr"],
            "uploaded_by": actor.employee_code}  # fmt: skip
    path.write_text(content, encoding="utf-8")
    path.with_name(path.name + ".meta.yaml").write_text(yaml.safe_dump(meta, sort_keys=False),
                                                         encoding="utf-8")  # fmt: skip
    result = ingest(session, root)
    doc = session.scalar(select(KnowledgeDocument).where(
        KnowledgeDocument.path == f"{UPLOADS}/{path.name}"))
    doc.uploaded_by_id = actor.id
    audit.record(session, "knowledge.upload", actor=actor, entity="knowledge_document",
                 entity_id=doc.id, new={"title": doc.title, "path": doc.path,
                                        "roles": doc.access_roles})  # fmt: skip
    return {**_doc_dict(doc), "ingest": result}


def remove_upload(session: Session, actor: Employee, doc_id: int, root: Path = KB_ROOT) -> None:
    d = session.get(KnowledgeDocument, doc_id)
    if d is None:
        raise NotFoundError("Document not found")
    if not d.path.startswith(UPLOADS + "/"):
        raise KnowledgeError("Only uploaded documents can be removed here; rule documents come "
                             "from the payroll configuration")  # fmt: skip
    path = root / d.path
    for p in (path, path.with_name(path.name + ".meta.yaml")):
        if p.exists():
            p.unlink()
    audit.record(session, "knowledge.remove", actor=actor, entity="knowledge_document",
                 entity_id=d.id, old={"title": d.title, "path": d.path})  # fmt: skip
    ingest(session, root)


def stats(session: Session, tenant: str = TENANT) -> dict[str, Any]:
    docs = session.scalar(select(func.count()).select_from(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == tenant)) or 0
    chunks = session.scalar(select(func.count()).select_from(KnowledgeChunk)
                            .join(KnowledgeDocument)
                            .where(KnowledgeDocument.tenant_id == tenant)) or 0  # fmt: skip
    unverified = session.scalar(select(func.count()).select_from(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == tenant, KnowledgeDocument.status != "VERIFIED")) or 0
    return {"documents": docs, "chunks": chunks, "unverified": unverified}

