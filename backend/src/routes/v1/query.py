from typing import Any

from fastapi import APIRouter

from ...handlers.query_handler import QueryHandler
from ...models.query_model import QueryRequest


def create_query_router(handler: QueryHandler) -> APIRouter:
    router = APIRouter()

    async def query(request: QueryRequest) -> dict[str, Any]:
        return await handler.query(request)

    router.add_api_route("/query", query, methods=["POST"], tags=["query"])
    return router


__all__ = ["create_query_router"]
