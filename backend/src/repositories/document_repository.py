from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ..core.config import Settings
from ..models.document_model import DocumentJob, DocumentRecord
from .embedding_repository import DeepInfraEmbeddingRepository
from .qdrant_repository import QdrantHttpClient, index_chunks


def sha256_file(path: str | Path, *, block_size: int = 1024 * 1024) -> str:
    """Return a stable SHA-256 fingerprint"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        while block := file.read(block_size):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def _connect(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_hash TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                ingestion_id TEXT NOT NULL,
                status TEXT NOT NULL,
                indexed_chunks INTEGER,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_document_id "
            "ON documents(document_id)"
        )
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        document_hash=str(row["document_hash"]),
        document_id=str(row["document_id"]),
        filename=str(row["filename"]),
        ingestion_id=str(row["ingestion_id"]),
        status=str(row["status"]),
        indexed_chunks=row["indexed_chunks"],
        error=row["error"],
    )


def reserve_document(
    database_path: str | Path,
    *,
    document_hash: str,
    document_id: str,
    filename: str,
    ingestion_id: str,
) -> DocumentRecord | None:
    """Atomically reserve a hash; return an existing non-failed record on duplicate."""
    with _connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM documents WHERE document_hash = ?",
            (document_hash,),
        ).fetchone()
        if row is not None and row["status"] != "failed":
            return _record(row)

        if row is None:
            connection.execute(
                """
                INSERT INTO documents (
                    document_hash, document_id, filename, ingestion_id, status
                ) VALUES (?, ?, ?, ?, 'queued')
                """,
                (document_hash, document_id, filename, ingestion_id),
            )
        else:
            # A failed ingestion did not create a usable corpus entry, so retrying
            # the same bytes is intentionally allowed.
            connection.execute(
                """
                UPDATE documents
                SET document_id = ?, filename = ?, ingestion_id = ?,
                    status = 'queued', indexed_chunks = NULL, error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE document_hash = ?
                """,
                (document_id, filename, ingestion_id, document_hash),
            )
    return None


def update_document_status(
    database_path: str | Path,
    *,
    document_hash: str,
    ingestion_id: str,
    status: str,
    indexed_chunks: int | None = None,
    error: str | None = None,
) -> None:
    """Update only the active reservation so an older worker cannot overwrite it."""
    with _connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE documents
            SET status = ?, indexed_chunks = ?, error = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE document_hash = ? AND ingestion_id = ?
            """,
            (status, indexed_chunks, error, document_hash, ingestion_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Reservasi dokumen tidak ditemukan atau sudah berubah")


def get_document(
    database_path: str | Path, document_hash: str
) -> DocumentRecord | None:
    """Read one registry entry for diagnostics and tests."""
    with _connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM documents WHERE document_hash = ?",
            (document_hash,),
        ).fetchone()
    return _record(row) if row is not None else None


class DocumentRepository:
    """Persist document state and chunks through injected storage clients."""

    def __init__(
        self,
        settings: Settings,
        database_path: str | Path,
        embedding_repository: DeepInfraEmbeddingRepository,
        vector_repository: QdrantHttpClient,
    ) -> None:
        self.settings = settings
        self.database_path = Path(database_path)
        self.embedding_repository = embedding_repository
        self.vector_repository = vector_repository
        self.jobs: dict[str, DocumentJob] = {}

    def create_job(self) -> DocumentJob:
        job = DocumentJob(job_id=uuid4().hex)
        self.jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str) -> DocumentJob | None:
        return self.jobs.get(job_id)

    def hash_file(self, path: str | Path) -> str:
        return sha256_file(path)

    def reserve(
        self,
        *,
        document_hash: str,
        document_id: str,
        filename: str,
        ingestion_id: str,
    ) -> DocumentRecord | None:
        return reserve_document(
            self.database_path,
            document_hash=document_hash,
            document_id=document_id,
            filename=filename,
            ingestion_id=ingestion_id,
        )

    def update_status(
        self,
        *,
        document_hash: str,
        ingestion_id: str,
        status: str,
        indexed_chunks: int | None = None,
        error: str | None = None,
    ) -> None:
        update_document_status(
            self.database_path,
            document_hash=document_hash,
            ingestion_id=ingestion_id,
            status=status,
            indexed_chunks=indexed_chunks,
            error=error,
        )

    async def index(
        self,
        chunks: list[dict[str, Any]],
        *,
        ingestion_id: str,
        on_progress: Callable[[str, int, int], Awaitable[None]] | None = None,
    ) -> int:
        return await index_chunks(
            chunks,
            self.embedding_repository,
            self.vector_repository,
            ingestion_id=ingestion_id,
            batch_size=self.settings.embedding_batch_size,
            on_progress=on_progress,
        )


__all__ = [
    "DocumentRecord",
    "DocumentRepository",
    "get_document",
    "reserve_document",
    "sha256_file",
    "update_document_status",
]
