"""Embedding model factory (local by default; free, offline after first download)."""

from functools import lru_cache

from langchain_core.embeddings import Embeddings

from payroll_copilot.core.config import PROJECT_ROOT, EmbeddingProvider, Settings, get_settings
from payroll_copilot.core.exceptions import ConfigurationError

MODEL_CACHE = PROJECT_ROOT / "data" / "models"


@lru_cache
def _local(model_name: str) -> Embeddings:
    from langchain_huggingface import HuggingFaceEmbeddings

    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    return HuggingFaceEmbeddings(
        model_name=model_name,
        cache_folder=str(MODEL_CACHE),
        encode_kwargs={"normalize_embeddings": True},
    )


def get_embeddings(settings: Settings | None = None) -> Embeddings:
    settings = settings or get_settings()
    if settings.embedding_provider is EmbeddingProvider.LOCAL:
        return _local(settings.embedding_model)
    raise ConfigurationError(
        f"EMBEDDING_PROVIDER={settings.embedding_provider.value} is not wired yet; use 'local'"
    )
