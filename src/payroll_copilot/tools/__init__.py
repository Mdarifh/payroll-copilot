"""LangChain tools over the deterministic payroll services (read-only).

Exports are resolved lazily so that importing ``payroll_copilot.tools.schemas`` does
not pull in the registry (LangChain, RAG, vector store) - that import is slow.
"""

from typing import Any

__all__ = ["TOOL_ACCESS", "ToolContext", "build_tools"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from payroll_copilot.tools import registry

        return getattr(registry, name)
    raise AttributeError(name)
