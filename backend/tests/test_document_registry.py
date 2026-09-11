import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from src.services.document_registry import (
    get_document,
    reserve_document,
    sha256_file,
    update_document_status,
)


class TestDocumentRegistry(unittest.TestCase):
    def test_rejects_same_file_hash_even_when_filename_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "registry.sqlite3"
            pdf = root / "aturan.pdf"
            pdf.write_bytes(b"same-pdf-content")
            document_hash = sha256_file(pdf)

            first = reserve_document(
                database,
                document_hash=document_hash,
                document_id="aturan",
                filename="aturan.pdf",
                ingestion_id="ingestion-1",
            )
            duplicate = reserve_document(
                database,
                document_hash=document_hash,
                document_id="nama_baru",
                filename="nama-baru.pdf",
                ingestion_id="ingestion-2",
            )

            self.assertIsNone(first)
            self.assertIsNotNone(duplicate)
            self.assertEqual(duplicate.filename, "aturan.pdf")
            self.assertEqual(duplicate.status, "queued")

    def test_allows_retry_after_failed_ingestion_and_tracks_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "registry.sqlite3"
            document_hash = "a" * 64
            reserve_document(
                database,
                document_hash=document_hash,
                document_id="aturan",
                filename="aturan.pdf",
                ingestion_id="ingestion-1",
            )
            update_document_status(
                database,
                document_hash=document_hash,
                ingestion_id="ingestion-1",
                status="failed",
                error="OCR gagal",
            )

            retry = reserve_document(
                database,
                document_hash=document_hash,
                document_id="aturan",
                filename="aturan.pdf",
                ingestion_id="ingestion-2",
            )
            update_document_status(
                database,
                document_hash=document_hash,
                ingestion_id="ingestion-2",
                status="completed",
                indexed_chunks=12,
            )
            completed = get_document(database, document_hash)

            self.assertIsNone(retry)
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.ingestion_id, "ingestion-2")
            self.assertEqual(completed.indexed_chunks, 12)
            self.assertIsNone(completed.error)


class TestDuplicateUploadResponse(unittest.IsolatedAsyncioTestCase):
    async def test_returns_http_409_when_the_only_upload_is_already_registered(self):
        from src.main import upload_documents

        content = b"identical-pdf-bytes"

        class InMemoryUpload:
            filename = "renamed.pdf"

            def __init__(self):
                self.file = io.BytesIO(content)

            async def seek(self, offset: int) -> None:
                self.file.seek(offset)

            async def close(self) -> None:
                self.file.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "document_registry.sqlite3"
            pdf = root / "existing.pdf"
            pdf.write_bytes(content)
            document_hash = sha256_file(pdf)
            reserve_document(
                database,
                document_hash=document_hash,
                document_id="existing",
                filename="existing.pdf",
                ingestion_id="ingestion-existing",
            )
            update_document_status(
                database,
                document_hash=document_hash,
                ingestion_id="ingestion-existing",
                status="completed",
                indexed_chunks=10,
            )
            upload = InMemoryUpload()

            with (
                patch("src.main.DOCUMENT_REGISTRY_PATH", database),
                patch("src.main.CHUNKS_DIR", root / "chunks"),
                patch("src.main.UPLOADS_DIR", root / "uploads"),
                self.assertRaises(HTTPException) as raised,
            ):
                await upload_documents([upload])

            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("sudah pernah diinput", str(raised.exception.detail))


if __name__ == "__main__":
    unittest.main()
