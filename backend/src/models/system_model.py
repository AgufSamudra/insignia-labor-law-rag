"""Data models owned by the system endpoints."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RootResponse:
    message: str


@dataclass(frozen=True)
class HealthResponse:
    status: str


__all__ = ["HealthResponse", "RootResponse"]
