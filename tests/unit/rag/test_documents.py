"""Metadata, loaders, chunking and generated rule documents."""

import zipfile
from datetime import date

import pytest
import yaml

from payroll_copilot.rag.chunking import chunk_documents
from payroll_copilot.rag.loaders import clean_text, discover_documents, load_document
from payroll_copilot.rag.metadata import (
    DocumentMetadata,
    KnowledgeBaseError,
    load_metadata,
    sidecar_path,
)
from payroll_copilot.rag.rule_docs import generate_rule_documents
from payroll_copilot.rules.loader import load_rule_files
from tests.conftest import RULES_DIR

META = {
    "module": "pf", "document_name": "Test", "version": "1", "effective_from": "2026-09-01",
    "jurisdiction": "IN", "source": "unit test",
}  # fmt: skip


def write_doc(folder, name, text, meta=META):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    sidecar_path(path).write_text(yaml.safe_dump(meta), encoding="utf-8")
    return path


class TestMetadata:
    def test_to_chroma_is_flat_and_numeric(self):
        meta = DocumentMetadata.model_validate(META).to_chroma()
        assert meta["effective_from_int"] == 20260901
        assert meta["effective_to_int"] == 99991231 and meta["effective_to"] == "open"
        assert all(isinstance(v, str | int) for v in meta.values())

    @pytest.mark.parametrize(("change", "message"), [
        ({"module": "salary"}, "module must be one of"),
        ({"jurisdiction": "Maharashtra"}, "String should match"),
        ({"effective_to": "2026-01-01"}, "effective_to before"),
        ({"extra": 1}, "Extra inputs"),
    ])  # fmt: skip
    def test_invalid(self, change, message):
        with pytest.raises(ValueError, match=message):
            DocumentMetadata.model_validate({**META, **change})

    def test_sidecar_required(self, tmp_path):
        (tmp_path / "pf").mkdir()
        doc = tmp_path / "pf" / "x.md"
        doc.write_text("# x", encoding="utf-8")
        with pytest.raises(KnowledgeBaseError, match="sidecar x.md.meta.yaml is missing"):
            load_metadata(doc, tmp_path)

    def test_folder_must_match_module(self, tmp_path):
        doc = write_doc(tmp_path / "esic", "x.md", "# x")
        with pytest.raises(KnowledgeBaseError, match="metadata module is 'pf'"):
            load_metadata(doc, tmp_path)


class TestLoaders:
    def test_clean_text(self):
        text = "Line one\r\n\r\n\r\n\r\nPage 3 of 10\n  extra   spaces \n12\n"
        assert clean_text(text) == "Line one\n\n extra spaces"

    def test_markdown_and_discovery(self, tmp_path):
        write_doc(tmp_path / "pf", "a.md", "# Title\nBody")
        (tmp_path / "README.md").write_text("ignored", encoding="utf-8")
        found = discover_documents(tmp_path)
        assert [p.name for p in found] == ["a.md"]
        assert load_document(found[0])[0].page_content == "# Title\nBody"

    def test_docx(self, tmp_path):
        path = tmp_path / "policy.docx"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
            z.writestr(
                "word/document.xml",
                '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>ESIC threshold policy'
                "</w:t></w:r></w:p></w:body></w:document>",
            )
        assert "ESIC threshold policy" in load_document(path)[0].page_content

    def test_pdf(self, tmp_path):
        content = b"BT /F1 12 Tf 72 720 Td (PF wage ceiling policy) Tj ET"
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        out, offsets = bytearray(b"%PDF-1.4\n"), []
        for i, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objects) + 1, xref)  # fmt: skip
        path = tmp_path / "rule.pdf"
        path.write_bytes(bytes(out))
        pages = load_document(path)
        assert "PF wage ceiling policy" in pages[0].page_content
        assert pages[0].metadata["page"] == 0

    def test_unsupported_and_empty(self, tmp_path):
        with pytest.raises(KnowledgeBaseError, match="Unsupported"):
            load_document(tmp_path / "x.xlsx")
        empty = tmp_path / "e.txt"
        empty.write_text("  \n Page 1 \n", encoding="utf-8")
        with pytest.raises(KnowledgeBaseError, match="no text"):
            load_document(empty)


def test_chunks_carry_metadata_and_headings(tmp_path):
    body = "# PF\n## Rates\n" + "Employee rate 12%. " * 120 + "\n## Ceiling\nPF wage ceiling."
    path = write_doc(tmp_path / "pf", "a.md", body)
    meta = DocumentMetadata.model_validate(META)
    chunks = chunk_documents(load_document(path), meta, markdown=True)
    assert len(chunks) > 2
    assert len({c.metadata["chunk_id"] for c in chunks}) == len(chunks)
    assert chunks[-1].metadata["section"] == "PF > Ceiling"
    assert all(c.metadata["effective_from_int"] == 20260901 for c in chunks)
    assert max(len(c.page_content) for c in chunks) <= 900


class TestGeneratedRuleDocs:
    def test_every_rule_version_becomes_a_document(self, tmp_path):
        paths = generate_rule_documents(load_rule_files(RULES_DIR), tmp_path)
        assert len(paths) == 43
        pf_v2 = (tmp_path / "pf" / "generated" / "epf_v2.md").read_text(encoding="utf-8")
        assert "PF wage ceiling: ₹25000 per month" in pf_v2
        assert "Effective: 2026-09-01 to open" in pf_v2 and "UNVERIFIED" in pf_v2
        meta = load_metadata(tmp_path / "pf" / "generated" / "epf_v1.md", tmp_path)
        assert (meta.effective_to, meta.jurisdiction, meta.version) == (date(2026, 8, 31), "IN", "1")

    def test_state_documents(self, tmp_path):
        generate_rule_documents(load_rule_files(RULES_DIR), tmp_path)
        mh = (tmp_path / "pt" / "generated" / "pt-mh_v1.md").read_text(encoding="utf-8")
        assert "## Slabs (gender F)" in mh and "month 2: ₹300" in mh
        dl = (tmp_path / "pt" / "generated" / "pt-dl_v1.md").read_text(encoding="utf-8")
        assert "not levied in Delhi" in dl
        assert load_metadata(tmp_path / "pt" / "generated" / "pt-mh_v1.md", tmp_path).jurisdiction == "IN-MH"

    def test_regeneration_removes_stale_files(self, tmp_path):
        stale = tmp_path / "pf" / "generated" / "old_rule.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("old", encoding="utf-8")
        generate_rule_documents(load_rule_files(RULES_DIR), tmp_path)
        assert not stale.exists()
