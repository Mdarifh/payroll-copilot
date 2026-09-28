import json
import logging

from payroll_copilot.__main__ import main
from payroll_copilot.core.logging import JsonFormatter, SecretRedactingFilter


def _record(msg, *args):
    return logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, None)


def test_redaction_masks_message_and_args():
    record = _record("key=%s used for %s", "sk-secret-1", "ARIF-112")
    SecretRedactingFilter(["sk-secret-1"]).filter(record)
    assert record.getMessage() == "key=**** used for ARIF-112"


def test_redaction_without_secrets_keeps_record():
    record = _record("hello %s", "world")
    SecretRedactingFilter([]).filter(record)
    assert record.getMessage() == "hello world"


def test_json_formatter():
    payload = json.loads(JsonFormatter().format(_record("PF ₹3,000")))
    assert payload["level"] == "INFO"
    assert payload["message"] == "PF ₹3,000"


def test_cli_check_config_masks_secret(monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-print-5678")
    assert main(["check-config"]) == 0
    out = capsys.readouterr().out
    assert "sk-should-not-print" not in out
    assert "****5678" in out
    assert "LLM_MODEL is not set" in out


def test_cli_reports_prod_config_error(monkeypatch, capsys):
    monkeypatch.setenv("APP_ENV", "prod")
    assert main(["check-config"]) == 1
    assert "Configuration error" in capsys.readouterr().out
