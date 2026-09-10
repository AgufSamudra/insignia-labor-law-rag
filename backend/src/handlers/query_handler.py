"""User-query endpoint handler."""

from typing import Any

try:
    from ..models.document import QueryRequest
except ImportError:
    from models.document import QueryRequest


async def query_user(request: QueryRequest) -> dict[str, Any]:
    """Delegate the existing query behavior through the handler layer."""
    try:
        from ..main import query_user as implementation
    except ImportError:
        from main import query_user as implementation
    return await implementation(request)


__all__ = ["query_user"]
