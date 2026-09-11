from ..models.system_model import HealthResponse, RootResponse

class SystemRepository:
    async def root(self) -> RootResponse:
        return RootResponse(
            message="Insignia Labor Law Assistant API is running"
        )

    async def health(self) -> HealthResponse:
        return HealthResponse(status="ok")


__all__ = ["SystemRepository"]
