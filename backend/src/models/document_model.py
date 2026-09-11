"""Data models owned by the document endpoints."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PendingDocument:
    """A saved PDF waiting for the background ingestion worker."""

    filename: str
    pdf_path: Path
    workspace: Path
    output_path: Path
    ingestion_id: str
    document_hash: str


@dataclass(frozen=True)
class DocumentRecord:
    """Persisted identity and ingestion state for one document."""

    document_hash: str
    document_id: str
    filename: str
    ingestion_id: str
    status: str
    indexed_chunks: int | None
    error: str | None


@dataclass
class ParserState:
    """Current legal hierarchy while document pages are parsed."""

    current_bab: str | None = None
    current_bab_title: str | None = None
    current_pasal: str | None = None
    current_ayat: str | None = None
    current_lines: list[str] = field(default_factory=list)
    current_pages: list[int] = field(default_factory=list)
    waiting_title: str | None = None
    section_part_counters: dict[tuple[str, str | None], int] = field(
        default_factory=dict
    )


@dataclass
class PageAnalysis:
    """Signals and extraction decision for one PDF page."""

    page_number: int
    native_text: str
    native_chars: int
    text_density: float
    image_count: int
    image_coverage: float
    native_pasal_count: int
    native_ayat_count: int
    suspicious_text: bool
    should_ocr: bool
    decision_reason: str
    final_extraction_mode: str | None = None
    final_chars: int | None = None


@dataclass
class PageExtractionResult:
    """Ordered extracted pages and their extraction analyses."""

    pages: list[tuple[int, str]]
    analyses: list[PageAnalysis]


@dataclass
class DocumentJob:
    """In-memory progress state for one upload request."""

    job_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    finished: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


__all__ = [
    "DocumentJob",
    "DocumentRecord",
    "PageAnalysis",
    "PageExtractionResult",
    "ParserState",
    "PendingDocument",
]
