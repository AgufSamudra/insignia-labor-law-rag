"""Document upload and progress endpoint handlers."""

from typing import Annotated, Any

from fastapi import File, UploadFile
from fastapi.responses import StreamingResponse


async def upload_documents(
    files: Annotated[
        list[UploadFile],
        File(description="One or more PDF documents to process"),
    ],
) -> dict[str, Any]:
    """Delegate upload processing without changing its existing behavior."""
    try:
        from ..main import upload_documents as implementation
    except ImportError:
        from main import upload_documents as implementation
    return await implementation(files)


async def document_events(job_id: str) -> StreamingResponse:
    """Stream progress events for one background upload job."""
    try:
        from ..main import document_events as implementation
    except ImportError:
        from main import document_events as implementation
    return await implementation(job_id)


async def document_status(job_id: str) -> dict[str, Any]:
    """Return event history for one background upload job."""
    try:
        from ..main import document_status as implementation
    except ImportError:
        from main import document_status as implementation
    return await implementation(job_id)


__all__ = ["document_events", "document_status", "upload_documents"]
