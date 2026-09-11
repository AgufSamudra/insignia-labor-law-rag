import logging
from typing import Any

from fastapi import HTTPException

from ..models.query_model import QueryRequest
from ..repositories.query_repository import QueryRepository


class QueryService:
    """Validate and execute one labor-law query."""

    def __init__(self, repository: QueryRepository) -> None:
        self.repository = repository
        self.logger = logging.getLogger(__name__)

    async def query(self, request: QueryRequest) -> dict[str, Any]:
        query = request.query.strip()
        if not query:
            raise HTTPException(status_code=422, detail="Query tidak boleh kosong")

        result = await self.repository.query(query)
        if result.retrieval.get("filtered"):
            self.logger.info("Query rejected by scope filter | query=%s", query)
        else:
            self.logger.info(
                "Query completed | query=%s retrieved_chunks=%s latency_ms=%s",
                query,
                result.retrieval.get("retrieved_chunks"),
                result.retrieval.get("latency_ms"),
            )
        return {
            "answer": result.answer,
            "sources": result.sources,
            "retrieval": result.retrieval,
        }


__all__ = ["QueryService"]
