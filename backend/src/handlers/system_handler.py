"""System endpoint handlers."""

from typing import Any


async def root() -> dict[str, str]:
    """Return basic information about the API."""
    try:
        from ..main import root as implementation
    except ImportError:
        from main import root as implementation
    return await implementation()


async def health_check() -> dict[str, str]:
    """Simple health check for local development and deployments."""
    try:
        from ..main import health_check as implementation
    except ImportError:
        from main import health_check as implementation
    return await implementation()


__all__ = ["health_check", "root"]
