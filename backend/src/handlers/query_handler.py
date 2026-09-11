from typing import Any

from ..models.query_model import QueryRequest
from ..services.query_service import QueryService


class QueryHandler:
    def __init__(self, service: QueryService) -> None:
        self.service = service

    async def query(self, request: QueryRequest) -> dict[str, Any]:
        return await self.service.query(request)


__all__ = ["QueryHandler"]
