from __future__ import annotations

from typing import Any

from ..core.config import Settings
from ..models.query_model import QueryResult
from ..prompt.query_prompt import OUT_OF_SCOPE_ANSWER
from ..utils.query.query_utils import QueryPipeline
from .embedding_repository import DeepInfraEmbeddingRepository
from .qdrant_repository import QdrantHttpClient


class QueryRepository:
    """Run query-related provider and vector-store operations."""

    def __init__(
        self,
        settings: Settings,
        embedding_repository: DeepInfraEmbeddingRepository,
        vector_repository: QdrantHttpClient,
        model_repository: Any,
    ) -> None:
        self.settings = settings
        self.embedding_repository = embedding_repository
        self.vector_repository = vector_repository
        self.model_repository = model_repository

    async def query(self, query: str) -> QueryResult:
        """Retrieve and generate one answer from external providers."""
        self.settings.validate_for_scope_filter()
        if not await self.model_repository.is_labor_law_question(query):
            return QueryResult(
                answer=OUT_OF_SCOPE_ANSWER,
                sources=[],
                retrieval={
                    "query": query,
                    "filtered": True,
                    "filter_reason": "outside_labor_law_scope",
                    "retrieved_chunks": 0,
                },
            )

        self.settings.validate_for_query()
        return await QueryPipeline(
            self.settings,
            self.embedding_repository,
            self.vector_repository,
            self.model_repository,
        ).run(query)


__all__ = ["QueryRepository"]
