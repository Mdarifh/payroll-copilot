"""Command line entry point: ``python -m payroll_copilot`` or ``payroll-chatbot``."""

import argparse
import sys
from pathlib import Path

from sqlalchemy import func, inspect, select

from payroll_copilot import __version__
from payroll_copilot.core.config import Settings, get_settings
from payroll_copilot.core.exceptions import ConfigurationError, PayrollCopilotError
from payroll_copilot.core.logging import setup_logging

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _check_config() -> int:
    try:
        settings = Settings()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        return 1
    setup_logging(settings)
    print(f"{settings.app_name} v{__version__}\n")
    print("Configuration (secrets masked):")
    summary = settings.summary()
    width = max(len(key) for key in summary)
    for key, value in summary.items():
        print(f"  {key:<{width}}  {value}")
    issues = settings.config_issues()
    if settings.jwt_secret is None:
        issues.append("JWT_SECRET not set: random key per run, logins end on API restart")
    print("\nWarnings:" if issues else "\nNo configuration issues found.")
    for issue in issues:
        print(f"  - {issue}")
    return 0


def _db_upgrade() -> int:
    from payroll_copilot.db.migrate import upgrade_to_head

    upgrade_to_head()
    print(f"Database migrated to head: {get_settings().database_url}")
    return 0


def _kb_ingest() -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.services.knowledge import ingest

    with session_scope() as session:
        result = ingest(session)
    print(f"Knowledge base: added {result['added']}, updated {result['updated']}, "
          f"unchanged {result['unchanged']}, removed {result['removed']}")
    if "embedded" in result:
        print(f"  semantic search: {result['embedded']} passages embedded")
    for error in result["errors"]:
        print(f"  skipped: {error}")
    return 0


def _rules_load() -> int:
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.rules.loader import load_rule_files, sync_rules
    from payroll_copilot.services.company_config import sync_defaults
    from payroll_copilot.services.statutory_rates import sync_rates

    files = load_rule_files(get_settings().rules_dir)
    with session_scope() as session:
        report = sync_rules(session, files)
        rates = sync_rates(session)
        settings_added = sync_defaults(session)
    print(f"Rules: {len(files)} files, {report.total} versions "
          f"(inserted {len(report.inserted)}, updated {len(report.updated)}, "
          f"unchanged {len(report.unchanged)})")  # fmt: skip
    print(rates)
    print(f"Company settings: {settings_added} default(s) added")
    return 0


def _db_upgrade_demo() -> int:
    from payroll_copilot.db.seed import upgrade_demo
    from payroll_copilot.db.session import session_scope

    _db_upgrade()
    _rules_load()
    with session_scope() as session:
        print(upgrade_demo(session))
    return 0


def _db_seed() -> int:
    from payroll_copilot.db.seed import DEFAULT_SEED_FILE, seed_demo_data
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.rules.loader import read_yaml

    _db_upgrade()
    _rules_load()
    with session_scope() as session:
        report = seed_demo_data(session)
    print(report)
    if not report.skipped:
        data = read_yaml(DEFAULT_SEED_FILE)
        print("\nDemo logins:")
        for e in data["employees"]:
            role = e.get("role", "employee")
            print(f"  {role:<8} {e['email']:<28} {data['passwords'][role]:<13} "
                  f"{e['employee_code']}  {e['scenario']}")  # fmt: skip
    return 0


def _db_add_dummy(count: int) -> int:
    from payroll_copilot.db.seed import add_dummy_employees
    from payroll_copilot.db.session import session_scope

    if not 1 <= count <= 10000:
        print("--count must be between 1 and 10000")
        return 1
    with session_scope() as session:
        report = add_dummy_employees(session, count)
    print(report)
    print("Login for any of them: <email shown in the HR employee list> / Employee@123")
    return 0


def _db_reset(confirmed: bool) -> int:
    settings = get_settings()
    if not settings.is_sqlite:
        print("db reset only supports SQLite dev databases.")
        return 1
    if not confirmed:
        print(f"This deletes {settings.database_url}. Re-run with --yes to confirm.")
        return 1
    from payroll_copilot.db.session import get_engine

    get_engine().dispose()
    path = Path(settings.database_url.removeprefix("sqlite:///"))
    path.unlink(missing_ok=True)
    print(f"Deleted {path}")
    return _db_seed()


def _db_status() -> int:
    import payroll_copilot.db.models  # noqa: F401
    from payroll_copilot.db.base import Base
    from payroll_copilot.db.session import get_engine, session_scope

    existing = set(inspect(get_engine()).get_table_names())
    with session_scope() as session:
        for name, table in sorted(Base.metadata.tables.items()):
            count = (session.scalar(select(func.count()).select_from(table))
                     if name in existing else "-")  # fmt: skip
            print(f"  {name:<20} {count}")
    return 0


def _api_serve(host: str | None, port: int | None) -> int:
    import uvicorn

    from payroll_copilot.api.main import create_app

    settings = get_settings()
    host = host or settings.api_host
    port = port or settings.api_port
    if host not in _LOOPBACK and settings.jwt_secret is None:
        print(f"Refusing to listen on {host} without JWT_SECRET; set it in .env or use "
              "127.0.0.1")  # fmt: skip
        return 1
    print(f"{settings.app_name} API on http://{host}:{port}  (docs: /docs)")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0


def _llm_check() -> int:
    """Is the configured LLM usable? Ollama: running + model pulled. Claude: key set."""
    import httpx

    from payroll_copilot.core.config import LLMProvider

    settings = get_settings()
    provider = settings.llm_provider
    print(f"LLM_PROVIDER={provider.value}  model={settings.llm_model or '-'}")
    if provider is LLMProvider.NONE:
        print("LLM switched off: chat answers that it is unavailable.")
        return 0
    if provider is LLMProvider.ANTHROPIC:
        if settings.anthropic_api_key is None:
            print("Missing ANTHROPIC_API_KEY in .env (create one at console.anthropic.com).")
            return 1
        print("ANTHROPIC_API_KEY is set. Note: Claude API usage is billed per request.")
        return 0
    try:
        tags = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5).json()
    except httpx.HTTPError:
        print(f"Ollama is not running at {settings.ollama_base_url}. Start the Ollama app "
              "(or `ollama serve`).")  # fmt: skip
        return 1
    names = {m.get("name") for m in tags.get("models", [])}
    if settings.ollama_model not in names and f"{settings.ollama_model}:latest" not in names:
        print(f"Model {settings.ollama_model} not pulled. Run: ollama pull {settings.ollama_model}")
        print(f"Available: {', '.join(sorted(n for n in names if n)) or 'none'}")
        return 1
    print("Ollama is running and the model is available.")
    return 0


def _chat(email: str) -> int:
    """Chat in the terminal as a demo user (same agent and tools as POST /chat)."""
    import getpass
    from datetime import date
    from uuid import uuid4

    from payroll_copilot.auth.service import login
    from payroll_copilot.chat.agent import ChatAgent
    from payroll_copilot.chat.tools import ToolContext
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.llm import get_chat_model

    password = getpass.getpass(f"Password for {email}: ")
    agent = ChatAgent(get_chat_model())
    conversation = uuid4().hex[:16]
    with session_scope() as session:
        user, _, _ = login(session, email, password)
        print(f"Logged in as {user.full_name} ({user.role.value}). Type 'exit' to quit.")
    while True:
        try:
            message = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if message.lower() in {"exit", "quit", "bye"}:
            break
        if not message:
            continue
        with session_scope() as session:
            user = session.merge(user)
            ctx = ToolContext(session=session, user=user, today=date.today())
            reply = agent.ask(ctx, message, conversation)
            session.add_all(ctx.audit_rows)
        for item in reply.messages:
            if item["type"] == "text":
                print(f"\nBot: {item['text']}")
            elif item["type"] == "payslip_card":
                slip = item["payslip"]
                print(f"  [payslip_card] {slip['period_label']}: gross {slip['gross_earnings']}, "
                      f"deductions {slip['total_deductions']}, net {slip['net_pay']}")
        if reply.tools_used:
            print(f"  (tools: {', '.join(reply.tools_used)})")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="payroll-chatbot", description="Payroll chatbot")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-config", help="Show the resolved configuration (secrets masked)")

    db = sub.add_parser("db", help="Database: upgrade | seed | reset | status")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("upgrade", help="Apply migrations")
    db_sub.add_parser("seed", help="Migrate, load rules and add demo data (idempotent)")
    reset = db_sub.add_parser("reset", help="Delete the SQLite DB and seed again")
    reset.add_argument("--yes", action="store_true", help="Confirm deletion")
    db_sub.add_parser("status", help="Row count per table")
    db_sub.add_parser("upgrade-demo", help="Migrate + add new demo people/settings, no reset")
    dummy = db_sub.add_parser("add-dummy", help="Add generated fake employees to the DB")
    dummy.add_argument("--count", type=int, default=50)

    rules = sub.add_parser("rules", help="Statutory rules")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_sub.add_parser("load", help="Validate config/rules/** and sync to the DB")

    kb = sub.add_parser("kb", help="Knowledge base (RAG documents)")
    kb_sub = kb.add_subparsers(dest="kb_command", required=True)
    kb_sub.add_parser("ingest", help="Index knowledge_base/ (new and changed documents)")

    llm = sub.add_parser("llm", help="LLM provider")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    llm_sub.add_parser("check", help="Is the configured model reachable?")

    chat = sub.add_parser("chat", help="Chat in the terminal as a demo user")
    chat.add_argument("email", help="Login email, e.g. rahul.verma@example.com")

    api = sub.add_parser("api", help="HTTP API")
    api_sub = api.add_subparsers(dest="api_command", required=True)
    serve = api_sub.add_parser("serve", help="Run the API (http://127.0.0.1:8000/docs)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252 (no ₹)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _build_parser().parse_args(argv)
    try:
        match args.command:
            case "check-config":
                return _check_config()
            case "db":
                commands = {"upgrade": _db_upgrade, "seed": _db_seed, "status": _db_status,
                            "reset": lambda: _db_reset(args.yes),
                            "add-dummy": lambda: _db_add_dummy(args.count),
                            "upgrade-demo": _db_upgrade_demo}  # fmt: skip
                return commands[args.db_command]()
            case "rules":
                return _rules_load()
            case "kb":
                return _kb_ingest()
            case "api":
                return _api_serve(args.host, args.port)
            case "llm":
                return _llm_check()
            case "chat":
                return _chat(args.email)
    except (PayrollCopilotError, ConfigurationError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
