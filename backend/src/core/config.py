"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

try:
    from .errors import ConfigurationError
except ImportError:  # Supports running this module directly from src/.
    from errors import ConfigurationError


# Loading the backend/.env file makes local `uvicorn src.main:app` behave the
# same as a deployment that injects environment variables. Existing variables
# always win, which is important in containers and CI.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigurationError(
            f"Environment variable {name} harus berupa integer"
        ) from error
    if parsed <= 0:
        raise ConfigurationError(
            f"Environment variable {name} harus lebih besar dari 0"
        )
    return parsed


@dataclass(frozen=True)
class Settings:
    """Runtime settings for embedding and vector indexing."""

    embedding_model: str
    deepinfra_api_key: str
    deepinfra_base_url: str
    qdrant_host: str
    qdrant_api_key: str | None
    qdrant_collection: str
    embedding_batch_size: int
    http_timeout_seconds: float
    reranked_model: str = "Qwen/Qwen3-Reranker-4B"
    generative_model: str = "Qwen/Qwen3-32B"
    retrieval_limit: int = 20
    rerank_top_k: int = 7
    rrf_k: int = 60
    rerank_min_score: float = 0.05
    query_rewrite_enabled: bool = True
    generation_max_tokens: int = 8192

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            embedding_model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3-multi").strip(),
            deepinfra_api_key=os.getenv("DEEPINFRA_API_KEY", "").strip(),
            deepinfra_base_url=os.getenv(
                "DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1"
            ).rstrip("/"),
            qdrant_host=os.getenv("QDRANT_HOST", "http://localhost:6333").rstrip("/"),
            qdrant_api_key=os.getenv("QDRANT_API_KEY", "").strip() or None,
            qdrant_collection=os.getenv(
                "QDRANT_COLLECTION", "insignia_labor_law_chunks"
            ).strip(),
            embedding_batch_size=_env_int("EMBEDDING_BATCH_SIZE", 8),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "120")),
            reranked_model=os.getenv(
                "RERANKED_MODEL", "Qwen/Qwen3-Reranker-4B"
            ).strip(),
            generative_model=os.getenv(
                "GENERATIVE_MODEL", "Qwen/Qwen3-32B"
            ).strip(),
            retrieval_limit=_env_int("RETRIEVAL_LIMIT", 20),
            rerank_top_k=_env_int("RERANK_TOP_K", 7),
            rrf_k=_env_int("RRF_K", 60),
            rerank_min_score=float(os.getenv("RERANK_MIN_SCORE", "0.05")),
            query_rewrite_enabled=os.getenv(
                "QUERY_REWRITE_ENABLED", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            generation_max_tokens=_env_int("GENERATION_MAX_TOKENS", 8192),
        )

    def validate_for_indexing(self) -> None:
        if not self.embedding_model:
            raise ConfigurationError("EMBEDDING_MODEL wajib diisi")
        if not self.deepinfra_api_key:
            raise ConfigurationError("DEEPINFRA_API_KEY wajib diisi")
        if not self.qdrant_host:
            raise ConfigurationError("QDRANT_HOST wajib diisi")
        if not self.qdrant_collection:
            raise ConfigurationError("QDRANT_COLLECTION wajib diisi")
        if self.http_timeout_seconds <= 0:
            raise ConfigurationError(
                "HTTP_TIMEOUT_SECONDS harus lebih besar dari 0"
            )

    def validate_for_query(self) -> None:
        """Validate configuration needed by the user-query pipeline."""
        self.validate_for_indexing()
        if not self.reranked_model:
            raise ConfigurationError("RERANKED_MODEL wajib diisi")
        if not self.generative_model:
            raise ConfigurationError("GENERATIVE_MODEL wajib diisi")
        if self.retrieval_limit <= 0:
            raise ConfigurationError("RETRIEVAL_LIMIT harus lebih besar dari 0")
        if self.rerank_top_k <= 0:
            raise ConfigurationError("RERANK_TOP_K harus lebih besar dari 0")
        if self.rrf_k <= 0:
            raise ConfigurationError("RRF_K harus lebih besar dari 0")
        if self.rerank_min_score < 0:
            raise ConfigurationError("RERANK_MIN_SCORE tidak boleh negatif")
        if self.generation_max_tokens <= 0 or self.generation_max_tokens > 40960:
            raise ConfigurationError(
                "GENERATION_MAX_TOKENS harus berada di antara 1 dan 40960"
            )

    def validate_for_scope_filter(self) -> None:
        """Validate only the configuration needed by the early scope filter."""
        if not self.deepinfra_api_key:
            raise ConfigurationError("DEEPINFRA_API_KEY wajib diisi")
        if not self.generative_model:
            raise ConfigurationError("GENERATIVE_MODEL wajib diisi")
        if self.http_timeout_seconds <= 0:
            raise ConfigurationError(
                "HTTP_TIMEOUT_SECONDS harus lebih besar dari 0"
            )
