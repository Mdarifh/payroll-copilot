"""Knowledge base facade used by the CLI, API and agent tools."""

from pathlib import Path

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from payroll_copilot.core.config import Settings, get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.rag.chain import PolicyAnswer, answer_question
from payroll_copilot.rag.embeddings import get_embeddings
from payroll_copilot.rag.ingest import IngestReport, ingest_knowledge_base
from payroll_copilot.rag.retriever import PolicyRetriever, RetrievedChunk
from payroll_copilot.rag.rule_docs import generate_rule_documents
from payroll_copilot.rag.vectorstore import KnowledgeStore
from payroll_copilot.rules.loader import load_rule_files


class KnowledgeBase:
    def __init__(
        self,
        settings: Settings | None = None,
        embeddings: Embeddings | None = None,
        persist_dir: Path | None = None,
        kb_root: Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.kb_root = kb_root or self.settings.knowledge_base_dir
        self.store = KnowledgeStore(
            embeddings or get_embeddings(self.settings),
            persist_dir if persist_dir is not None else self.settings.vector_store_dir,
        )
        self.retriever = PolicyRetriever(self.store)

    def generate(self, rules_dir: Path | None = None) -> list[Path]:
        files = load_rule_files(rules_dir or self.settings.rules_dir)
        return generate_rule_documents(files, self.kb_root)

    def ingest(self, rebuild: bool = False) -> IngestReport:
        return ingest_knowledge_base(self.kb_root, self.store, rebuild=rebuild)

    def search(
        self,
        query: str,
        *,
        module: str | None = None,
        period: PayrollPeriod | None = None,
        state: str | None = None,
        k: int = 4,
    ) -> list[RetrievedChunk]:
        return self.retriever.retrieve(query, module=module, period=period, state=state, k=k)

    def ask(
        self,
        question: str,
        *,
        module: str | None = None,
        period: PayrollPeriod | None = None,
        state: str | None = None,
        k: int = 4,
        llm: BaseChatModel | None = None,
    ) -> PolicyAnswer:
        chunks = self.search(question, module=module, period=period, state=state, k=k)
        return answer_question(question, chunks, llm)
