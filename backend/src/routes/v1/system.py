from fastapi import APIRouter

from ...handlers.system_handler import SystemHandler
from ...models.system_model import HealthResponse, RootResponse


def create_system_router(handler: SystemHandler) -> APIRouter:
    router = APIRouter()

    async def root() -> RootResponse:
        return await handler.root()

    async def health() -> HealthResponse:
        return await handler.health()

    router.add_api_route("/", root, methods=["GET"], tags=["system"])
    router.add_api_route("/health", health, methods=["GET"], tags=["system"])
    return router


__all__ = ["create_system_router"]
