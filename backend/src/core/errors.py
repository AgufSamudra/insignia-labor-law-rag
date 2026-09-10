"""Application-specific exceptions used by the backend pipeline."""


class InsigniaError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(InsigniaError):
    """Raised when required environment configuration is missing or invalid."""


class DocumentProcessingError(InsigniaError):
    """Raised when a document cannot be extracted or parsed."""


class EmbeddingServiceError(InsigniaError):
    """Raised when DeepInfra cannot return a valid embedding response."""


class VectorStoreError(InsigniaError):
    """Raised when Qdrant cannot create or write the vector index."""


class QueryServiceError(InsigniaError):
    """Raised when reranking or answer generation fails."""
