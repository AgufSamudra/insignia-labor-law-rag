"""System routes."""

from fastapi import APIRouter

try:
    from ...handlers.system_handler import health_check, root
except ImportError:
    from handlers.system_handler import health_check, root


router = APIRouter()
router.add_api_route("/", root, methods=["GET"], tags=["system"])
router.add_api_route("/health", health_check, methods=["GET"], tags=["system"])

__all__ = ["router"]
