"""Document upload and progress routes."""

from fastapi import APIRouter

try:
    from ...handlers.document_handler import (
        document_events,
        document_status,
        upload_documents,
    )
except ImportError:
    from handlers.document_handler import document_events, document_status, upload_documents


router = APIRouter()
for path in ("/upload_document", "/upload-documents", "/documents/upload"):
    router.add_api_route(path, upload_documents, methods=["POST"], tags=["documents"])
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

__all__ = ["router"]
