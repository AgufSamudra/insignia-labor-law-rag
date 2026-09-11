from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.config import Settings
from .core.exception_handlers import register_exception_handlers
from .handlers.document_handler import DocumentHandler
from .handlers.query_handler import QueryHandler
from .handlers.system_handler import SystemHandler
from .repositories.document_repository import DocumentRepository
from .repositories.embedding_repository import DeepInfraEmbeddingRepository
from .repositories.qdrant_repository import QdrantHttpClient
from .repositories.query_repository import QueryRepository
from .repositories.system_repository import SystemRepository
from .routes.v1.router import create_api_router
from .services.document_service import DocumentService
from .services.query_service import QueryService
from .services.system_service import SystemService
from .utils.document.upload_pipeline_utils import DocumentUploadPipeline
from .utils.logging import configure_logging
from .utils.query.query_utils import DeepInfraQueryClient


DATA_DIRECTORY = Path(__file__).resolve().parents[1] / "data"
CHUNKS_DIRECTORY = DATA_DIRECTORY / "chunks"
UPLOADS_DIRECTORY = DATA_DIRECTORY / "uploads"
DOCUMENT_REGISTRY_PATH = DATA_DIRECTORY / "document_registry.sqlite3"


def create_app() -> FastAPI:
    """Initialize configuration, dependencies, routes, and application lifecycle."""
    configure_logging()
    settings = Settings.from_env()

    embedding_repository = DeepInfraEmbeddingRepository(settings)
    vector_repository = QdrantHttpClient(settings)
    model_repository = DeepInfraQueryClient(settings)

    query_repository = QueryRepository(
        settings,
        embedding_repository,
        vector_repository,
        model_repository,
    )
    query_handler = QueryHandler(QueryService(query_repository))

    document_repository = DocumentRepository(
        settings,
        DOCUMENT_REGISTRY_PATH,
        embedding_repository,
        vector_repository,
    )
    document_upload_pipeline = DocumentUploadPipeline(
        settings,
        document_repository,
        CHUNKS_DIRECTORY,
        UPLOADS_DIRECTORY,
    )
    document_handler = DocumentHandler(
        DocumentService(document_repository, document_upload_pipeline)
    )

    system_handler = SystemHandler(SystemService(SystemRepository()))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await model_repository.aclose()
        await embedding_repository.aclose()
        await vector_repository.aclose()

    application = FastAPI(
        title="Insignia Labor Law Assistant API",
        description="API backend for the Insignia labor law assistant.",
        version="0.1.0",
        docs_url="/v1/docs",
        redoc_url="/v1/redoc",
        openapi_url="/v1/openapi.json",
        swagger_ui_oauth2_redirect_url="/v1/docs/oauth2-redirect",
        lifespan=lifespan,
    )
    register_exception_handlers(application)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(
        create_api_router(
            system_handler=system_handler,
            query_handler=query_handler,
            document_handler=document_handler,
        )
    )
    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.main:app", host="127.0.0.1", port=8000, reload=True)
