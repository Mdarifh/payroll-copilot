"""Answer policy questions from retrieved chunks, always with sources.

With an LLM: the model may only use the numbered context and must cite it; a
grounding check flags numbers in the answer that do not appear in the context.
Without an LLM (LLM_PROVIDER=none): an extractive answer of the top excerpts.
"""

import re

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.rag.retriever import RetrievedChunk

NOT_FOUND = "I could not find this in the payroll knowledge base for the given period/filters."

SYSTEM_PROMPT = """You are a payroll policy assistant for an Indian payroll team.
Rules:
- Answer ONLY from the numbered context below. Never use outside knowledge.
- Cite the context numbers you used, like [1] or [2][3].
- If the context does not contain the answer, reply exactly: "{not_found}"
- Do not compute payroll amounts yourself; say the calculation engine computes them.
- Mention the rule version and effective dates when you state a rule.
- Keep the answer short and clear.

Context:
{context}"""

PROMPT = ChatPromptTemplate.from_messages(
    [("system", SYSTEM_PROMPT), ("human", "{question}")]
)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


class PolicyAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    question: str
    answer: str
    sources: list[str]
    used_llm: bool
    found: bool
    warnings: list[str] = Field(default_factory=list)


def format_context(chunks: list[RetrievedChunk]) -> str:
    return "\n\n".join(f"[{i}] ({c.citation})\n{c.text}" for i, c in enumerate(chunks, start=1))


def ungrounded_numbers(answer: str, context: str) -> list[str]:
    """Numbers in the answer that never appear in the context (commas ignored)."""
    known = {n.replace(",", "") for n in _NUMBER.findall(context)}
    cited = {str(i) for i in range(1, 50)}  # citation markers like [1]
    return sorted(
        {n for n in _NUMBER.findall(answer) if n.replace(",", "") not in known | cited}
    )


def answer_question(
    question: str, chunks: list[RetrievedChunk], llm: BaseChatModel | None = None
) -> PolicyAnswer:
    sources = [f"[{i}] {c.citation}" for i, c in enumerate(chunks, start=1)]
    if not chunks:
        return PolicyAnswer(question=question, answer=NOT_FOUND, sources=[], used_llm=False,
                            found=False)  # fmt: skip
    context = format_context(chunks)
    if llm is None:
        excerpts = "\n".join(
            f"[{i}] {c.text[:400]}{'…' if len(c.text) > 400 else ''}"
            for i, c in enumerate(chunks, start=1)
        )
        return PolicyAnswer(
            question=question, answer=f"Relevant excerpts (LLM disabled):\n{excerpts}",
            sources=sources, used_llm=False, found=True,
        )  # fmt: skip
    text = (PROMPT | llm | StrOutputParser()).invoke(
        {"question": question, "context": context, "not_found": NOT_FOUND}
    ).strip()
    warnings = []
    missing = ungrounded_numbers(text, context)
    if missing:
        warnings.append(f"Answer contains numbers not present in the sources: {missing}")
    return PolicyAnswer(
        question=question, answer=text, sources=sources, used_llm=True,
        found=NOT_FOUND not in text, warnings=warnings,
    )  # fmt: skip
