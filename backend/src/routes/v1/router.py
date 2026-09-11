from fastapi import APIRouter

from ...handlers.document_handler import DocumentHandler
from ...handlers.query_handler import QueryHandler
from ...handlers.system_handler import SystemHandler
from .document import create_document_router
from .query import create_query_router
from .system import create_system_router


def create_api_router(
    *,
    system_handler: SystemHandler,
    query_handler: QueryHandler,
    document_handler: DocumentHandler,
) -> APIRouter:
    router = APIRouter(prefix="/v1")
    router.include_router(create_system_router(system_handler))
    router.include_router(create_query_router(query_handler))
    router.include_router(create_document_router(document_handler))
    return router


__all__ = ["create_api_router"]
