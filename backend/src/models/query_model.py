"""Data models owned by the query endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """Request body for the user-query endpoint."""

    query: str = Field(min_length=1, max_length=2000)


@dataclass(frozen=True)
class Embedding:
    """Dense and sparse vectors belonging to one text."""

    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]


@dataclass
class RetrievedChunk:
    """A vector-store payload enriched with retrieval scores."""

    point_id: str
    payload: dict[str, Any]
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float = 0.0

    @property
    def chunk_id(self) -> str:
        return str(self.payload.get("chunk_id") or self.point_id)

    @property
    def text(self) -> str:
        return str(self.payload.get("text", "")).strip()


@dataclass
class QueryResult:
    """Serializable result of the complete query pipeline."""

    answer: str
    sources: list[dict[str, Any]]
    retrieval: dict[str, Any]


__all__ = ["Embedding", "QueryRequest", "QueryResult", "RetrievedChunk"]
