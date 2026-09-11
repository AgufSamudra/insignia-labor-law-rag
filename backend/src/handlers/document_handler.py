from typing import Any

from fastapi import UploadFile
from fastapi.responses import StreamingResponse

from ..services.document_service import DocumentService


class DocumentHandler:
    def __init__(self, service: DocumentService) -> None:
        self.service = service

    async def upload_documents(self, files: list[UploadFile]) -> dict[str, Any]:
        return await self.service.upload_documents(files)

    async def document_events(self, job_id: str) -> StreamingResponse:
        return await self.service.document_events(job_id)

    async def document_status(self, job_id: str) -> dict[str, Any]:
        return await self.service.document_status(job_id)


__all__ = ["DocumentHandler"]
