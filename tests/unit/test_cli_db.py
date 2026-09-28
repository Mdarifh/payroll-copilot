from payroll_copilot.__main__ import main
from payroll_copilot.core.config import get_settings


def test_seed_resolve_status_reset(temp_db_url, capsys, tmp_path, monkeypatch):
    assert main(["db", "seed"]) == 0
    out = capsys.readouterr().out
    assert "inserted 43" in out
    assert "employees=10" in out

    assert main(["db", "seed"]) == 0
    assert "nothing seeded" in capsys.readouterr().out

    assert main(["rules", "resolve", "PF", "--period", "2026-09", "--employee", "ARIF-112"]) == 0
    out = capsys.readouterr().out
    assert "EPF v2 (2026-09-01 to open)" in out
    assert "pf_wage_ceiling              25000" in out
    assert "UNVERIFIED" in out

    assert main(["rules", "resolve", "PF", "--period", "2026-09", "--employee", "EMP-007"]) == 1
    assert "No PF rule for EMP-007" in capsys.readouterr().out

    assert main(["rules", "resolve", "PF", "--period", "2026-09", "--employee", "NOPE"]) == 1

    assert main(["salary", "show", "EMP-005", "--period", "2026-09"]) == 0
    out = capsys.readouterr().out
    assert "active in period 2026-09-15 to 2026-09-30" in out

    assert main(["salary", "breakup", "--structure", "STD-2026", "--gross", "33333"]) == 0
    assert "Result: ₹33,333" in capsys.readouterr().out

    assert main(["salary", "breakup", "--structure", "NOPE", "--gross", "1"]) == 1
    assert "not found" in capsys.readouterr().out

    assert main(["salary", "calc", "EMP-005", "--period", "2026-09", "--trace"]) == 0
    out = capsys.readouterr().out
    assert "GROSS EARNINGS" in out and "₹18,667" in out
    assert "payable 16 − LOP 0 = 16 days" in out

    assert main(["pf", "calc", "ARIF-112", "--period", "2026-09", "--trace"]) == 0
    out = capsys.readouterr().out
    assert "3. PF wage: min(30000, 25000) = ₹25,000" in out

    assert main(["pf", "compare", "ARIF-112", "--period", "2026-09"]) == 0
    assert "EPF v2 (2026-09-01 to open) *" in capsys.readouterr().out

    assert main(["esic", "calc", "EMP-003", "--period", "2026-09", "--trace"]) == 0
    assert "COVERED" in capsys.readouterr().out

    assert main(["payroll", "calc", "ARIF-112", "--period", "2026-09"]) == 0
    assert "₹56,800" in capsys.readouterr().out  # 60000 − PF 3000 − PT 200

    assert main(["arrear", "calc", "EMP-004", "--period", "2026-09"]) == 0
    out = capsys.readouterr().out
    assert "(27500 − 25000) × 31 / 31" in out and "PF on arrear (EPF v1)" in out

    assert main(["fnf", "calc", "EMP-006"]) == 0
    assert "NET F&F SETTLEMENT: ₹34,000" in capsys.readouterr().out

    assert main(["db", "seed-actual"]) == 0
    assert "10 payslips" in capsys.readouterr().out
    assert main(["validate", "payroll", "--period", "2026-09"]) == 0
    assert "with issues: 7" in capsys.readouterr().out
    assert main(["validate", "employee", "ARIF-112", "--period", "2026-09"]) == 0
    assert "[RULE_VERSION]" in capsys.readouterr().out

    assert main(["qa", "run", "--period", "2026-09"]) == 0
    assert "Findings: 8" in capsys.readouterr().out
    assert main(["qa", "history"]) == 0
    assert "No saved QA runs" in capsys.readouterr().out  # plain run does not save
    assert main(["qa", "run", "--period", "2026-09", "--save"]) == 0
    assert "Approval request #1" in capsys.readouterr().out  # --save needs a checker now
    assert main(["qa", "show", "1"]) == 1  # nothing saved yet
    capsys.readouterr()
    assert main(["approval", "list", "--status", "PENDING"]) == 0
    assert "Save the QA run for 2026-09" in capsys.readouterr().out
    assert main(["approval", "approve", "1", "--as", "checker"]) == 1  # no APPROVERS configured
    assert "No approvers configured" in capsys.readouterr().out
    monkeypatch.setenv("APPROVERS", '["checker"]')
    get_settings.cache_clear()
    assert main(["approval", "approve", "1", "--as", "checker", "--comment", "ok"]) == 0
    assert "EXECUTED" in capsys.readouterr().out
    assert main(["qa", "show", "1"]) == 0
    assert "RULE_VERSION" in capsys.readouterr().out
    report_file = tmp_path / "qa.csv"
    assert main(["qa", "run", "--period", "2026-09", "--format", "csv", "--out", str(report_file)]) == 0
    assert report_file.read_text(encoding="utf-8").count("\n") == 9  # header + 8 findings

    assert main(["audit", "list", "--action", "qa.save"]) == 0
    assert "qa.save" in capsys.readouterr().out  # the --save above was audited
    assert main(["audit", "list", "--action", "payroll.seed_actual"]) == 0
    assert "payroll.seed_actual" in capsys.readouterr().out
    assert main(["audit", "show", "999999"]) == 1
    capsys.readouterr()

    assert main(["attendance", "show", "EMP-002", "--period", "2026-09"]) == 0
    assert "Paid days 27 of 30" in capsys.readouterr().out

    assert main(["attendance", "diagnose", "EMP-002", "--period", "2026-09", "--actual", "30"]) == 0
    assert "LOP of 3 days not deducted" in capsys.readouterr().out

    assert main(["attendance", "show", "EMP-005", "--period", "2026-08"]) == 1
    assert "No attendance" in capsys.readouterr().out

    assert main(["db", "status"]) == 0
    assert "employees" in capsys.readouterr().out

    assert main(["db", "reset"]) == 1
    assert "--yes" in capsys.readouterr().out
    assert main(["db", "reset", "--yes"]) == 0
    assert "employees=10" in capsys.readouterr().out
