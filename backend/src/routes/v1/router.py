"""API route registry following the Kerjiva route layout."""

from fastapi import APIRouter

try:
    from .documents import router as documents_router
    from .query import router as query_router
    from .system import router as system_router
except ImportError:
    from routes.v1.documents import router as documents_router
    from routes.v1.query import router as query_router
    from routes.v1.system import router as system_router


api_router = APIRouter()
api_router.include_router(system_router)
api_router.include_router(query_router)
api_router.include_router(documents_router)

__all__ = ["api_router"]
