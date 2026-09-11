from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import StreamingResponse

from ...handlers.document_handler import DocumentHandler


def create_document_router(handler: DocumentHandler) -> APIRouter:
    router = APIRouter()

    async def upload_documents(
        files: Annotated[
            list[UploadFile],
            File(description="One or more PDF documents to process"),
        ],
    ) -> dict[str, Any]:
        return await handler.upload_documents(files)

    async def document_events(job_id: str) -> StreamingResponse:
        return await handler.document_events(job_id)

    async def document_status(job_id: str) -> dict[str, Any]:
        return await handler.document_status(job_id)

    router.add_api_route(
        "/documents/upload",
        upload_documents,
        methods=["POST"],
        tags=["documents"],
    )
    router.add_api_route(
        "/documents/{job_id}/events",
        document_events,
        methods=["GET"],
        tags=["documents"],
    )
    router.add_api_route(
        "/documents/{job_id}/status",
        document_status,
        methods=["GET"],
        tags=["documents"],
    )
    return router


__all__ = ["create_document_router"]
