"""User-query routes."""

from fastapi import APIRouter

try:
    from ...handlers.query_handler import query_user
except ImportError:
    from handlers.query_handler import query_user


router = APIRouter()
router.add_api_route("/v1/query", query_user, methods=["POST"], tags=["query"])

__all__ = ["router"]
