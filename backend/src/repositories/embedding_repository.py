from __future__ import annotations

import logging
import math
from typing import Any

import httpx

try:
    from ..core.config import Settings
    from ..core.errors import EmbeddingServiceError
    from ..models.query_model import Embedding
except ImportError:  # Supports running this module directly from src/.
    from core.config import Settings
    from core.errors import EmbeddingServiceError
    from models.query_model import Embedding


logger = logging.getLogger(__name__)


class DeepInfraEmbeddingRepository:
    """HTTPX client for DeepInfra's native BGE-M3 endpoint."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds)
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def embed(self, texts: list[str]) -> list[Embedding]:
        """Embed a batch of texts and return dense+sparse vectors."""
        if not texts:
            return []
        self.settings.validate_for_indexing()

        logger.info(
            "Embedding request started | model=%s texts=%d",
            self.settings.embedding_model,
            len(texts),
        )
        body = await self._request(texts)

        dense_rows = body.get("embeddings")
        sparse_rows = body.get("sparse")
        if not isinstance(dense_rows, list) or len(dense_rows) != len(texts):
            raise EmbeddingServiceError(
                "Jumlah dense embedding dari DeepInfra tidak sesuai jumlah input"
            )
        if not isinstance(sparse_rows, list) or len(sparse_rows) != len(texts):
            raise EmbeddingServiceError(
                "Respons DeepInfra tidak mengandung sparse embedding lengkap"
            )

        result: list[Embedding] = []
        for index, (dense_row, sparse_row) in enumerate(
            zip(dense_rows, sparse_rows)
        ):
            sparse_indices, sparse_values = _parse_sparse(sparse_row, index)
            result.append(
                Embedding(
                    dense=_parse_dense(dense_row, index),
                    sparse_indices=sparse_indices,
                    sparse_values=sparse_values,
                )
            )

        logger.info(
            "Embedding request completed | model=%s texts=%d dense_dimensions=%d",
            self.settings.embedding_model,
            len(result),
            len(result[0].dense),
        )
        return result

    async def _request(self, texts: list[str]) -> dict[str, Any]:
        """Send one native DeepInfra request and return its JSON body."""
        url = (
            f"{self.settings.deepinfra_base_url}/inference/"
            f"{self.settings.embedding_model}"
        )
        try:
            response = await self._client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.settings.deepinfra_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "inputs": texts,
                    "dense": True,
                    "sparse": True,
                    "colbert": False,
                    "normalize": True,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise EmbeddingServiceError(
                f"DeepInfra embedding gagal (HTTP {error.response.status_code})"
            ) from error
        except httpx.RequestError as error:
            raise EmbeddingServiceError(
                "Tidak dapat terhubung ke layanan embedding DeepInfra"
            ) from error

        try:
            body = response.json()
        except ValueError as error:
            raise EmbeddingServiceError(
                "Respons DeepInfra bukan JSON yang valid"
            ) from error

        if not isinstance(body, dict):
            raise EmbeddingServiceError("Respons DeepInfra harus berupa object JSON")
        return body


def _parse_dense(row: Any, item: int) -> list[float]:
    """Validate one dense embedding row."""
    if not isinstance(row, list) or not row:
        raise EmbeddingServiceError(f"Dense embedding item {item} kosong atau invalid")

    try:
        vector = [float(value) for value in row]
    except (TypeError, ValueError) as error:
        raise EmbeddingServiceError(
            f"Dense embedding item {item} mengandung nilai invalid"
        ) from error

    if any(not math.isfinite(value) for value in vector):
        raise EmbeddingServiceError(
            f"Dense embedding item {item} mengandung nilai invalid"
        )
    return vector


def _parse_sparse(row: Any, item: int) -> tuple[list[int], list[float]]:
    """Convert BGE-M3 sparse output to Qdrant's indices/values format."""
    if isinstance(row, dict):
        pairs = row.items()
    elif isinstance(row, list):
        # A full vocabulary row contains mostly zero values.
        pairs = enumerate(row)
    else:
        raise EmbeddingServiceError(f"Sparse embedding item {item} tidak valid")

    parsed_pairs: list[tuple[int, float]] = []
    try:
        for index, value in pairs:
            index = int(index)
            value = float(value)
            if index < 0 or not math.isfinite(value):
                raise ValueError("sparse value invalid")
            if value != 0:
                parsed_pairs.append((index, value))
    except (TypeError, ValueError) as error:
        raise EmbeddingServiceError(
            f"Sparse embedding item {item} mengandung nilai invalid"
        ) from error

    parsed_pairs.sort(key=lambda pair: pair[0])
    return (
        [index for index, _ in parsed_pairs],
        [value for _, value in parsed_pairs],
    )
