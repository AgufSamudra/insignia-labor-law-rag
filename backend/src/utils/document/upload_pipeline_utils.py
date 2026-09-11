import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from ...core.config import Settings
from ...core.errors import DocumentProcessingError
from ...models.document_model import DocumentJob, PendingDocument
from ...repositories.document_repository import DocumentRepository
from .document_utils import (
    make_document_id,
    process_pdf_document,
)
from .progress_utils import publish_job_event


logger = logging.getLogger(__name__)
STAGE_MESSAGES = {
    "upload": "File diterima dan menunggu proses",
    "parsing": "Membaca PDF dan menjalankan OCR bila diperlukan",
    "embedding": "Membuat dense + sparse embedding di DeepInfra",
    "indexing": "Menyimpan vector dan metadata ke Qdrant",
    "completed": "Dokumen selesai diproses",
    "failed": "Pemrosesan dokumen gagal",
}


class DocumentUploadPipeline:
    """Coordinate the ordered upload-to-Qdrant pipeline.

    Call sequence:
    one -> two -> three -> four -> five -> six -> seven.
    """

    def __init__(self, settings: Settings,
        repository: DocumentRepository, chunks_directory: Path, uploads_directory: Path,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.chunks_directory = chunks_directory
        self.uploads_directory = uploads_directory
        self.background_tasks: set[asyncio.Task[None]] = set()

    async def one_validate_and_save_uploaded_files(
        self,
        files: list[UploadFile],
    ) -> dict[str, Any]:
        """Step 1: validate, save, fingerprint, and reserve uploaded PDFs."""
        self.chunks_directory.mkdir(parents=True, exist_ok=True)
        job = self.repository.create_job()
        job_directory = self.uploads_directory / job.job_id
        job_directory.mkdir(parents=True, exist_ok=True)
        pending_documents: list[PendingDocument] = []
        failed_files: list[dict[str, str]] = []
        logger.info("Upload accepted | job_id=%s files=%d", job.job_id, len(files))

        try:
            for uploaded_file in files:
                original_filename = Path(
                    uploaded_file.filename or "document.pdf"
                ).name
                if Path(original_filename).suffix.lower() != ".pdf":
                    error_message = "Hanya file PDF yang dapat diproses"
                    failed_files.append(
                        {"filename": original_filename, "error": error_message}
                    )
                    await publish_job_event(
                        job,
                        event_type="document_status",
                        filename=original_filename,
                        stage="failed",
                        status="failed",
                        message=f"{STAGE_MESSAGES['failed']}: {error_message}",
                        error=error_message,
                    )
                    await uploaded_file.close()
                    continue

                workspace = job_directory / uuid4().hex
                workspace.mkdir()
                pdf_path = workspace / original_filename
                output_path = self.chunks_directory / (
                    f"{make_document_id(original_filename)}-{uuid4().hex[:8]}.jsonl"
                )
                ingestion_id = uuid4().hex
                document_hash: str | None = None
                registry_reserved = False
                try:
                    await uploaded_file.seek(0)
                    with pdf_path.open("wb") as destination:
                        shutil.copyfileobj(uploaded_file.file, destination)
                    document_hash = self.repository.hash_file(pdf_path)
                    existing_document = self.repository.reserve(
                        document_hash=document_hash,
                        document_id=make_document_id(original_filename),
                        filename=original_filename,
                        ingestion_id=ingestion_id,
                    )
                    if existing_document is not None:
                        error_message = (
                            "Dokumen sudah pernah diinput "
                            f"(status: {existing_document.status}, "
                            f"file: {existing_document.filename})"
                        )
                        failed_files.append(
                            {
                                "filename": original_filename,
                                "error": error_message,
                                "code": "duplicate_document",
                                "existing_status": existing_document.status,
                            }
                        )
                        await publish_job_event(
                            job,
                            event_type="document_status",
                            filename=original_filename,
                            stage="failed",
                            status="failed",
                            message=f"{STAGE_MESSAGES['failed']}: {error_message}",
                            error=error_message,
                            error_code="duplicate_document",
                        )
                        shutil.rmtree(workspace, ignore_errors=True)
                        continue
                    registry_reserved = True
                    pending_documents.append(
                        PendingDocument(
                            filename=original_filename,
                            pdf_path=pdf_path,
                            workspace=workspace,
                            output_path=output_path,
                            ingestion_id=ingestion_id,
                            document_hash=document_hash,
                        )
                    )
                    await publish_job_event(
                        job,
                        event_type="document_status",
                        filename=original_filename,
                        stage="upload",
                        status="queued",
                        message=STAGE_MESSAGES["upload"],
                    )
                except Exception as error:
                    if registry_reserved and document_hash is not None:
                        try:
                            self.repository.update_status(
                                document_hash=document_hash,
                                ingestion_id=ingestion_id,
                                status="failed",
                                error=str(error),
                            )
                        except Exception:
                            logger.exception(
                                "Could not mark upload reservation as failed | filename=%s",
                                original_filename,
                            )
                    logger.exception(
                        "Uploaded file could not be saved | job_id=%s filename=%s",
                        job.job_id,
                        original_filename,
                    )
                    failed_files.append(
                        {"filename": original_filename, "error": str(error)}
                    )
                    await publish_job_event(
                        job,
                        event_type="document_status",
                        filename=original_filename,
                        stage="failed",
                        status="failed",
                        message=f"{STAGE_MESSAGES['failed']}: {error}",
                        error=str(error),
                    )
                    shutil.rmtree(workspace, ignore_errors=True)
                finally:
                    await uploaded_file.close()
        except Exception:
            shutil.rmtree(job_directory, ignore_errors=True)
            raise

        if pending_documents:
            self.two_schedule_background_processing(job, pending_documents)
        else:
            await publish_job_event(
                job,
                event_type="job_status",
                filename="-",
                stage="job",
                status="failed",
                message="Tidak ada dokumen valid untuk diproses",
            )
            shutil.rmtree(job_directory, ignore_errors=True)
            if failed_files and all(
                item.get("code") == "duplicate_document" for item in failed_files
            ):
                detail = (
                    failed_files[0]["error"]
                    if len(failed_files) == 1
                    else "Semua dokumen sudah pernah diinput"
                )
                raise HTTPException(status_code=409, detail=detail)

        return {
            "job_id": job.job_id,
            "status": "queued" if pending_documents else "failed",
            "message": "Upload diterima dan pemrosesan berjalan di background",
            "files": [
                {"filename": document.filename, "status": "queued"}
                for document in pending_documents
            ],
            "failed": failed_files,
        }

    def remember_background_task(self, task: asyncio.Task[None]) -> None:
        self.background_tasks.add(task)

        def on_done(completed_task: asyncio.Task[None]) -> None:
            self.background_tasks.discard(completed_task)
            if completed_task.cancelled():
                return
            error = completed_task.exception()
            if error:
                logger.error(
                    "Background job crashed unexpectedly | error=%s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(on_done)

    def set_registry_status(
        self,
        document: PendingDocument,
        status: str,
        *,
        indexed_chunks: int | None = None,
        error: str | None = None,
    ) -> None:
        try:
            self.repository.update_status(
                document_hash=document.document_hash,
                ingestion_id=document.ingestion_id,
                status=status,
                indexed_chunks=indexed_chunks,
                error=error,
            )
        except Exception:
            logger.exception(
                "Document registry update failed | filename=%s ingestion_id=%s status=%s",
                document.filename,
                document.ingestion_id,
                status,
            )

    def two_schedule_background_processing(
        self,
        job: DocumentJob,
        documents: list[PendingDocument],
    ) -> None:
        """Step 2: schedule accepted documents for background processing."""
        task = asyncio.create_task(
            self.three_process_document_job(job, documents)
        )
        self.remember_background_task(task)
        logger.info(
            "Background job scheduled | job_id=%s documents=%d",
            job.job_id,
            len(documents),
        )

    async def three_process_document_job(
        self,
        job: DocumentJob,
        documents: list[PendingDocument],
    ) -> None:
        """Step 3: process every accepted document in the upload job."""
        successful_documents = 0
        for document in documents:
            try:
                self.set_registry_status(document, "processing")
                await publish_job_event(
                    job,
                    event_type="document_status",
                    filename=document.filename,
                    stage="parsing",
                    status="processing",
                    message=STAGE_MESSAGES["parsing"],
                )
                event_loop = asyncio.get_running_loop()
                extraction_counts = {"native_text": 0, "full_ocr": 0}

                def report_page(current: int, total: int, mode: str) -> None:
                    percent = round((current / total) * 100) if total else 0
                    extraction_counts[mode] += 1
                    native_pages = extraction_counts["native_text"]
                    ocr_pages = extraction_counts["full_ocr"]
                    operation = (
                        "Full OCR" if mode == "full_ocr" else "Membaca native text"
                    )
                    future = asyncio.run_coroutine_threadsafe(
                        publish_job_event(
                            job,
                            event_type="document_status",
                            filename=document.filename,
                            stage="parsing",
                            status="processing",
                            message=(
                                f"{operation} · halaman {current}/{total} · "
                                f"Native {native_pages} · OCR {ocr_pages}"
                            ),
                            progress_current=current,
                            progress_total=total,
                            progress_percent=percent,
                            progress_unit="halaman",
                            extraction_mode=mode,
                            native_pages=native_pages,
                            ocr_pages=ocr_pages,
                            total_pages=total,
                        ),
                        event_loop,
                    )
                    future.result()

                chunks = await self.four_extract_document_chunks(
                    document,
                    report_page,
                )
                await publish_job_event(
                    job,
                    event_type="document_status",
                    filename=document.filename,
                    stage="parsing",
                    status="processing",
                    message=(
                        f"{STAGE_MESSAGES['parsing']} · {len(chunks)} chunk ditemukan · "
                        f"Native {extraction_counts['native_text']} halaman · "
                        f"OCR {extraction_counts['full_ocr']} halaman"
                    ),
                    chunks=len(chunks),
                    native_pages=extraction_counts["native_text"],
                    ocr_pages=extraction_counts["full_ocr"],
                    total_pages=sum(extraction_counts.values()),
                )

                indexed_chunks = await self.five_save_vectors_to_qdrant(
                    job,
                    document,
                    chunks,
                )
                successful_documents += 1
                await self.six_mark_document_completed(
                    job,
                    document,
                    chunks,
                    indexed_chunks,
                    extraction_counts,
                )
            except Exception as error:
                self.set_registry_status(document, "failed", error=str(error))
                logger.exception(
                    "Document background processing failed | job_id=%s filename=%s "
                    "ingestion_id=%s",
                    job.job_id,
                    document.filename,
                    document.ingestion_id,
                )
                await publish_job_event(
                    job,
                    event_type="document_status",
                    filename=document.filename,
                    stage="failed",
                    status="failed",
                    message=f"{STAGE_MESSAGES['failed']}: {error}",
                    error=str(error),
                )
            finally:
                shutil.rmtree(document.workspace, ignore_errors=True)

        await self.seven_finalize_upload_job(
            job,
            document_count=len(documents),
            successful_documents=successful_documents,
        )

    async def four_extract_document_chunks(
        self,
        document: PendingDocument,
        report_page: Callable[[int, int, str], None],
    ) -> list[dict[str, Any]]:
        """Step 4: extract/OCR the PDF and build legal document chunks."""
        chunks = await run_in_threadpool(
            process_pdf_document,
            document.pdf_path,
            document.output_path,
            on_page_progress=report_page,
        )
        if not chunks:
            raise DocumentProcessingError(
                "Parsing PDF tidak menghasilkan chunk; "
                "periksa format heading Pasal atau hasil OCR"
            )
        return chunks

    async def five_save_vectors_to_qdrant(
        self,
        job: DocumentJob,
        document: PendingDocument,
        chunks: list[dict[str, Any]],
    ) -> int:
        """Step 5: embed chunks and persist their vectors in Qdrant."""
        async def report_progress(stage: str, current: int, total: int) -> None:
            percent = round((current / total) * 100) if total else 0
            await publish_job_event(
                job,
                event_type="document_status",
                filename=document.filename,
                stage=stage,
                status="processing",
                message=f"{STAGE_MESSAGES[stage]} · {current}/{total} chunk",
                progress_current=current,
                progress_total=total,
                progress_percent=percent,
                progress_unit="chunk",
            )

        return await self.repository.index(
            chunks,
            ingestion_id=document.ingestion_id,
            on_progress=report_progress,
        )

    async def six_mark_document_completed(
        self,
        job: DocumentJob,
        document: PendingDocument,
        chunks: list[dict[str, Any]],
        indexed_chunks: int,
        extraction_counts: dict[str, int],
    ) -> None:
        """Step 6: persist and publish successful document completion."""
        self.set_registry_status(
            document,
            "completed",
            indexed_chunks=indexed_chunks,
        )
        await publish_job_event(
            job,
            event_type="document_status",
            filename=document.filename,
            stage="completed",
            status="completed",
            message=(
                f"{STAGE_MESSAGES['completed']} · "
                f"{indexed_chunks} chunk masuk ke Qdrant"
            ),
            chunks=len(chunks),
            indexed_chunks=indexed_chunks,
            native_pages=extraction_counts["native_text"],
            ocr_pages=extraction_counts["full_ocr"],
            total_pages=sum(extraction_counts.values()),
            output_file=document.output_path.name,
            qdrant_collection=self.settings.qdrant_collection,
        )

    async def seven_finalize_upload_job(
        self,
        job: DocumentJob,
        *,
        document_count: int,
        successful_documents: int,
    ) -> None:
        """Step 7: publish the terminal status for the complete upload job."""
        job_status = "completed" if successful_documents else "failed"
        job_message = (
            "Pemrosesan upload selesai"
            if successful_documents == document_count
            else "Pemrosesan upload selesai, beberapa dokumen gagal"
            if successful_documents
            else "Semua dokumen gagal diproses"
        )
        await publish_job_event(
            job,
            event_type="job_status",
            filename="-",
            stage="job",
            status=job_status,
            message=job_message,
        )
