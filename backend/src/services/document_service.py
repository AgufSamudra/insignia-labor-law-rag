from typing import Any

from fastapi import HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..repositories.document_repository import DocumentRepository
from ..utils.document.progress_utils import stream_job_events
from ..utils.document.upload_pipeline_utils import DocumentUploadPipeline


class DocumentService:
    def __init__(
        self,
        repository: DocumentRepository,
        upload_pipeline: DocumentUploadPipeline,
    ) -> None:
        self.repository = repository
        self.upload_pipeline = upload_pipeline

    async def upload_documents(self, files: list[UploadFile]) -> dict[str, Any]:
        return await self.upload_pipeline.one_validate_and_save_uploaded_files(files)

    async def document_events(self, job_id: str) -> StreamingResponse:
        job = self.repository.get_job(job_id)
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

    async def document_status(self, job_id: str) -> dict[str, Any]:
        job = self.repository.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job upload tidak ditemukan")
        async with job.lock:
            events = list(job.events)
        return {"job_id": job_id, "events": events}


__all__ = ["DocumentService"]
