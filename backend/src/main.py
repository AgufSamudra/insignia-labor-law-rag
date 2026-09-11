import asyncio
import logging
import shutil
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

try:
    from .core.config import Settings
    from .core.errors import DocumentProcessingError
    from .core.exception_handlers import register_exception_handlers
    from .models.document import PendingDocument, QueryRequest
    from .prompt.query_prompt import OUT_OF_SCOPE_ANSWER
    from .repositories.qdrant_repository import QdrantHttpClient, index_chunks
    from .services.document_service import make_document_id, process_pdf_document
    from .services.document_registry import (
        reserve_document,
        sha256_file,
        update_document_status,
    )
    from .services.embedding_service import DeepInfraEmbeddingClient
    from .services.progress_service import (
        STAGE_MESSAGES,
        DocumentJob,
        create_job,
        get_job,
        stream_job_events,
    )
    from .routes.v1.router import api_router
    from .services.query_service import DeepInfraQueryClient, QueryPipeline
    from .utils.logging import configure_logging
except ImportError:  # Supports running this file directly with `python src/main.py`.
    from core.config import Settings
    from core.errors import DocumentProcessingError
    from core.exception_handlers import register_exception_handlers
    from models.document import PendingDocument, QueryRequest
    from prompt.query_prompt import OUT_OF_SCOPE_ANSWER
    from repositories.qdrant_repository import QdrantHttpClient, index_chunks
    from routes.v1.router import api_router
    from services.document_service import make_document_id, process_pdf_document
    from services.document_registry import (
        reserve_document,
        sha256_file,
        update_document_status,
    )
    from services.embedding_service import DeepInfraEmbeddingClient
    from services.progress_service import (
        STAGE_MESSAGES,
        DocumentJob,
        create_job,
        get_job,
        stream_job_events,
    )
    from services.query_service import DeepInfraQueryClient, QueryPipeline
    from utils.logging import configure_logging


configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Insignia Labor Law Assistant API",
    description="API backend for the Insignia labor law assistant.",
    version="0.1.0",
)
register_exception_handlers(app)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CHUNKS_DIR = DATA_DIR / "chunks"
UPLOADS_DIR = DATA_DIR / "uploads"
DOCUMENT_REGISTRY_PATH = DATA_DIR / "document_registry.sqlite3"
BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

# Allow the Vite development server to call the API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


def remember_background_task(task: asyncio.Task[None]) -> None:
    """Keep a strong reference to a running task and log unexpected failures."""
    BACKGROUND_TASKS.add(task)

    def on_done(completed_task: asyncio.Task[None]) -> None:
        BACKGROUND_TASKS.discard(completed_task)
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
    document: PendingDocument,
    status: str,
    *,
    indexed_chunks: int | None = None,
    error: str | None = None,
) -> None:
    """Persist lifecycle state without hiding the underlying document result."""
    try:
        update_document_status(
            DOCUMENT_REGISTRY_PATH,
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


async def process_document_job(
    job: DocumentJob,
    documents: list[PendingDocument],
) -> None:
    """Process each saved PDF and publish the same status that FE displays."""
    try:
        settings = Settings.from_env()
        embedding_client = DeepInfraEmbeddingClient(settings)
        qdrant_client = QdrantHttpClient(settings)
    except Exception as error:
        logger.exception("Background job could not start | job_id=%s", job.job_id)
        for document in documents:
            set_registry_status(document, "failed", error=str(error))
            shutil.rmtree(document.workspace, ignore_errors=True)
            await job.publish(
                event_type="document_status",
                filename=document.filename,
                stage="failed",
                status="failed",
                message=f"{STAGE_MESSAGES['failed']}: {error}",
                error=str(error),
            )
        await job.publish(
            event_type="job_status",
            filename="-",
            stage="job",
            status="failed",
            message="Semua dokumen gagal diproses",
        )
        return

    successful_documents = 0
    try:
        for document in documents:
            try:
                set_registry_status(document, "processing")
                await job.publish(
                    event_type="document_status",
                    filename=document.filename,
                    stage="parsing",
                    status="processing",
                    message=STAGE_MESSAGES["parsing"],
                )

                event_loop = asyncio.get_running_loop()

                def report_page(current: int, total: int, mode: str) -> None:
                    percent = round((current / total) * 100) if total else 0
                    operation = (
                        "Full OCR" if mode == "full_ocr" else "Membaca native text"
                    )
                    future = asyncio.run_coroutine_threadsafe(
                        job.publish(
                            event_type="document_status",
                            filename=document.filename,
                            stage="parsing",
                            status="processing",
                            message=f"{operation} · halaman {current}/{total}",
                            progress_current=current,
                            progress_total=total,
                            progress_percent=percent,
                            progress_unit="halaman",
                            extraction_mode=mode,
                        ),
                        event_loop,
                    )
                    future.result()

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

                await job.publish(
                    event_type="document_status",
                    filename=document.filename,
                    stage="parsing",
                    status="processing",
                    message=(
                        f"{STAGE_MESSAGES['parsing']} · "
                        f"{len(chunks)} chunk ditemukan"
                    ),
                    chunks=len(chunks),
                )

                async def report_progress(
                    stage: str,
                    current: int,
                    total: int,
                ) -> None:
                    percent = round((current / total) * 100) if total else 0
                    await job.publish(
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

                indexed_chunks = await index_chunks(
                    chunks,
                    embedding_client,
                    qdrant_client,
                    ingestion_id=document.ingestion_id,
                    batch_size=settings.embedding_batch_size,
                    on_progress=report_progress,
                )
                set_registry_status(
                    document,
                    "completed",
                    indexed_chunks=indexed_chunks,
                )
                successful_documents += 1
                await job.publish(
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
                    output_file=document.output_path.name,
                    qdrant_collection=settings.qdrant_collection,
                )
            except Exception as error:
                set_registry_status(document, "failed", error=str(error))
                logger.exception(
                    "Document background processing failed | job_id=%s filename=%s "
                    "ingestion_id=%s",
                    job.job_id,
                    document.filename,
                    document.ingestion_id,
                )
                await job.publish(
                    event_type="document_status",
                    filename=document.filename,
                    stage="failed",
                    status="failed",
                    message=f"{STAGE_MESSAGES['failed']}: {error}",
                    error=str(error),
                )
            finally:
                shutil.rmtree(document.workspace, ignore_errors=True)
    finally:
        await embedding_client.aclose()
        await qdrant_client.aclose()

    job_status = "completed" if successful_documents else "failed"
    job_message = (
        "Pemrosesan upload selesai"
        if successful_documents == len(documents)
        else "Pemrosesan upload selesai, beberapa dokumen gagal"
        if successful_documents
        else "Semua dokumen gagal diproses"
    )
    await job.publish(
        event_type="job_status",
        filename="-",
        stage="job",
        status=job_status,
        message=job_message,
    )


async def root() -> dict[str, str]:
    """Return basic information about the API."""
    return {"message": "Insignia Labor Law Assistant API is running"}


async def health_check() -> dict[str, str]:
    """Simple health check for local development and deployments."""
    return {"status": "ok"}


async def query_user(request: QueryRequest) -> dict[str, Any]:
    """Answer one labor-law question using hybrid retrieval and grounded generation."""
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Query tidak boleh kosong")

    settings = Settings.from_env()
    settings.validate_for_scope_filter()
    model_client = DeepInfraQueryClient(settings)
    try:
        if not await model_client.is_labor_law_question(query):
            logger.info("Query rejected by scope filter | query=%s", query)
            return {
                "answer": OUT_OF_SCOPE_ANSWER,
                "sources": [],
                "retrieval": {
                    "query": query,
                    "filtered": True,
                    "filter_reason": "outside_labor_law_scope",
                    "retrieved_chunks": 0,
                },
            }

        settings.validate_for_query()
        embedding_client = DeepInfraEmbeddingClient(settings)
        qdrant_client = QdrantHttpClient(settings)
        try:
            result = await QueryPipeline(
                settings,
                embedding_client,
                qdrant_client,
                model_client,
            ).run(query)
            logger.info(
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
        finally:
            await embedding_client.aclose()
            await qdrant_client.aclose()
    finally:
        await model_client.aclose()


async def upload_documents(
    files: Annotated[
        list[UploadFile],
        File(description="One or more PDF documents to process"),
    ],
) -> dict[str, Any]:
    """Save PDFs, return immediately, and process them in the background."""
    CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
    job = create_job()
    job_directory = UPLOADS_DIR / job.job_id
    job_directory.mkdir(parents=True, exist_ok=True)
    pending_documents: list[PendingDocument] = []
    failed_files: list[dict[str, str]] = []

    logger.info("Upload accepted | job_id=%s files=%d", job.job_id, len(files))

    try:
        for uploaded_file in files:
            original_filename = Path(uploaded_file.filename or "document.pdf").name
            if Path(original_filename).suffix.lower() != ".pdf":
                error_message = "Hanya file PDF yang dapat diproses"
                failed_files.append(
                    {"filename": original_filename, "error": error_message}
                )
                await job.publish(
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
            output_filename = (
                f"{make_document_id(original_filename)}-{uuid4().hex[:8]}.jsonl"
            )
            output_path = CHUNKS_DIR / output_filename
            ingestion_id = uuid4().hex
            document_hash: str | None = None
            registry_reserved = False

            try:
                await uploaded_file.seek(0)
                with pdf_path.open("wb") as destination:
                    shutil.copyfileobj(uploaded_file.file, destination)
                document_hash = sha256_file(pdf_path)
                document_id = make_document_id(original_filename)
                existing_document = reserve_document(
                    DOCUMENT_REGISTRY_PATH,
                    document_hash=document_hash,
                    document_id=document_id,
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
                    await job.publish(
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
                document = PendingDocument(
                    filename=original_filename,
                    pdf_path=pdf_path,
                    workspace=workspace,
                    output_path=output_path,
                    ingestion_id=ingestion_id,
                    document_hash=document_hash,
                )
                pending_documents.append(document)
                await job.publish(
                    event_type="document_status",
                    filename=original_filename,
                    stage="upload",
                    status="queued",
                    message=STAGE_MESSAGES["upload"],
                )
            except Exception as error:
                if registry_reserved and document_hash is not None:
                    try:
                        update_document_status(
                            DOCUMENT_REGISTRY_PATH,
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
                await job.publish(
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
        task = asyncio.create_task(process_document_job(job, pending_documents))
        remember_background_task(task)
        logger.info(
            "Background job scheduled | job_id=%s documents=%d",
            job.job_id,
            len(pending_documents),
        )
    else:
        await job.publish(
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


async def document_events(job_id: str) -> StreamingResponse:
    """Stream document progress events for one background upload job."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job upload tidak ditemukan")

    return StreamingResponse(
        stream_job_events(job),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def document_status(job_id: str) -> dict[str, Any]:
    """Return the event history used as a fallback when SSE reconnects."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job upload tidak ditemukan")

    async with job.lock:
        events = list(job.events)
    return {"job_id": job_id, "events": events}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
