from payroll_copilot.__main__ import main
from payroll_copilot.core.config import get_settings


def test_kb_cli(monkeypatch, capsys, tmp_path, kb_copy, fake_embeddings):
    monkeypatch.setenv("KNOWLEDGE_BASE_DIR", str(kb_copy))
    monkeypatch.setenv("VECTOR_STORE_DIR", str(tmp_path / "chroma"))
    get_settings.cache_clear()
    monkeypatch.setattr("payroll_copilot.rag.service.get_embeddings", lambda settings: fake_embeddings)

    assert main(["kb", "ingest", "--generate", "--rebuild"]) == 0
    out = capsys.readouterr().out
    assert "Generated 43 rule documents" in out and "Ingested 48 documents" in out

    assert main(["kb", "search", "PF wage ceiling", "--module", "pf", "--period", "2026-08"]) == 0
    out = capsys.readouterr().out
    assert "EPF v1" in out and "EPF v2" not in out

    assert main(["kb", "ask", "tax slabs", "--module", "tds", "--period", "2027-05"]) == 0
    assert "could not find" in capsys.readouterr().out

    assert main(["kb", "ask", "LOP reversal", "--module", "attendance"]) == 0
    out = capsys.readouterr().out
    assert "Relevant excerpts (LLM disabled)" in out and "Sources:" in out
