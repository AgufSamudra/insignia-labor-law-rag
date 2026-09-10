"""Request and document models used by the HTTP layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class PendingDocument:
    """A saved PDF waiting for the background ingestion worker."""

    filename: str
    pdf_path: Path
    workspace: Path
    output_path: Path
    ingestion_id: str


class QueryRequest(BaseModel):
    """Request body for the user-query endpoint."""

    query: str = Field(min_length=1, max_length=2000)
