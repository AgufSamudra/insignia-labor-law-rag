from ..models.system_model import HealthResponse, RootResponse
from ..repositories.system_repository import SystemRepository


class SystemService:
    def __init__(self, repository: SystemRepository) -> None:
        self.repository = repository

    async def root(self) -> RootResponse:
        return await self.repository.root()

    async def health(self) -> HealthResponse:
        return await self.repository.health()


__all__ = ["SystemService"]
