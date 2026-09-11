from ..models.system_model import HealthResponse, RootResponse
from ..services.system_service import SystemService


class SystemHandler:
    def __init__(self, service: SystemService) -> None:
        self.service = service

    async def root(self) -> RootResponse:
        return await self.service.root()

    async def health(self) -> HealthResponse:
        return await self.service.health()


__all__ = ["SystemHandler"]
