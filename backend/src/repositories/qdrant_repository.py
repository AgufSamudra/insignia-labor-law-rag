"""Small Qdrant REST client implemented with HTTPX."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

try:
    from ..core.config import Settings
    from ..core.errors import VectorStoreError
    from ..services.embedding_service import Embedding
except ImportError:  # Supports running this module directly from src/.
    from core.config import Settings
    from core.errors import VectorStoreError
    from services.embedding_service import Embedding


logger = logging.getLogger(__name__)


class QdrantHttpClient:
    """Qdrant REST operations required by the ingestion pipeline."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        headers = {"Content-Type": "application/json"}
        if settings.qdrant_api_key:
            headers["api-key"] = settings.qdrant_api_key
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds), headers=headers
        )
        if client is not None:
            self._client.headers.update(headers)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ensure_collection(self, dense_size: int) -> None:
        path = f"/collections/{self.settings.qdrant_collection}"
        logger.info(
            "Checking Qdrant collection | collection=%s",
            self.settings.qdrant_collection,
        )
        try:
            response = await self._client.get(f"{self.settings.qdrant_host}{path}")
        except httpx.HTTPError as error:
            raise VectorStoreError(
                "Tidak dapat terhubung ke Qdrant saat membaca collection"
            ) from error
        if response.status_code == 404:
            try:
                create_response = await self._client.put(
                    f"{self.settings.qdrant_host}{path}",
                    json={
                        "vectors": {
                            "dense": {"size": dense_size, "distance": "Cosine"}
                        },
                        "sparse_vectors": {"sparse": {"modifier": "idf"}},
                    },
                )
            except httpx.HTTPError as error:
                raise VectorStoreError(
                    "Tidak dapat terhubung ke Qdrant saat membuat collection"
                ) from error
            self._raise_for_error(create_response, "membuat collection Qdrant")
            logger.info(
                "Qdrant collection created | collection=%s dense_dimensions=%d",
                self.settings.qdrant_collection,
                dense_size,
            )
            return

        self._raise_for_error(response, "membaca collection Qdrant")
        try:
            body = response.json()
            vectors = body["result"]["config"]["params"]["vectors"]
            dense_config = vectors["dense"]
            existing_size = dense_config["size"]
        except (KeyError, TypeError, ValueError) as error:
            raise VectorStoreError(
                "Konfigurasi collection Qdrant tidak dapat dibaca"
            ) from error
        if existing_size != dense_size:
            raise VectorStoreError(
                f"Dimensi dense Qdrant ({existing_size}) berbeda dari model ({dense_size}); "
                "gunakan collection baru atau embed model yang sama"
            )
        logger.info(
            "Qdrant collection ready | collection=%s dense_dimensions=%d",
            self.settings.qdrant_collection,
            dense_size,
        )

    async def upsert(
        self,
        chunks: list[dict[str, Any]],
        embeddings: list[Embedding],
        *,
        ingestion_id: str,
    ) -> None:
        if len(chunks) != len(embeddings):
            raise VectorStoreError("Jumlah chunk dan embedding tidak sama")
        points: list[dict[str, Any]] = []
        for chunk, embedding in zip(chunks, embeddings):
            chunk_id = str(chunk.get("chunk_id", ""))
            point_id = str(uuid5(NAMESPACE_URL, f"{ingestion_id}:{chunk_id}"))
            payload = dict(chunk)
            payload.update(
                {
                    "point_id": point_id,
                    "ingestion_id": ingestion_id,
                    "embedding_model": self.settings.embedding_model,
                }
            )
            points.append(
                {
                    "id": point_id,
                    "vector": {
                        "dense": embedding.dense,
                        "sparse": {
                            "indices": embedding.sparse_indices,
                            "values": embedding.sparse_values,
                        },
                    },
                    "payload": payload,
                }
            )

        logger.info(
            "Qdrant upsert started | collection=%s points=%d ingestion_id=%s",
            self.settings.qdrant_collection,
            len(points),
            ingestion_id,
        )
        try:
            response = await self._client.put(
                f"{self.settings.qdrant_host}/collections/"
                f"{self.settings.qdrant_collection}/points?wait=true",
                json={"points": points},
            )
        except httpx.HTTPError as error:
            raise VectorStoreError(
                "Tidak dapat terhubung ke Qdrant saat menyimpan vector"
            ) from error
        self._raise_for_error(response, "menyimpan vector ke Qdrant")
        logger.info(
            "Qdrant upsert completed | collection=%s points=%d ingestion_id=%s",
            self.settings.qdrant_collection,
            len(points),
            ingestion_id,
        )

    async def search(
        self,
        vector_name: str,
        vector: list[float] | dict[str, list[int] | list[float]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Search one named dense or sparse vector and return payloads."""
        if limit <= 0:
            return []
        body = {
            "vector": {"name": vector_name, "vector": vector},
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        }
        try:
            response = await self._client.post(
                f"{self.settings.qdrant_host}/collections/"
                f"{self.settings.qdrant_collection}/points/search",
                json=body,
            )
        except httpx.HTTPError as error:
            raise VectorStoreError(
                f"Tidak dapat terhubung ke Qdrant saat melakukan {vector_name} search"
            ) from error
        self._raise_for_error(response, f"melakukan {vector_name} search di Qdrant")
        try:
            result = response.json()["result"]
        except (KeyError, TypeError, ValueError) as error:
            raise VectorStoreError(
                "Respons search Qdrant tidak memiliki result yang valid"
            ) from error
        if not isinstance(result, list):
            raise VectorStoreError("Result search Qdrant harus berupa list")
        return [item for item in result if isinstance(item, dict)]

    async def scroll_parent(
        self,
        *,
        filename: str,
        pasal: str,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        """Load chunks belonging to one Pasal for lightweight parent expansion."""
        points: list[dict[str, Any]] = []
        offset: str | int | None = None
        while len(points) < limit:
            body: dict[str, Any] = {
                "filter": {
                    "must": [
                        {"key": "filename", "match": {"value": filename}},
                        {"key": "pasal", "match": {"value": pasal}},
                    ]
                },
                "limit": min(128, limit - len(points)),
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            try:
                response = await self._client.post(
                    f"{self.settings.qdrant_host}/collections/"
                    f"{self.settings.qdrant_collection}/points/scroll",
                    json=body,
                )
            except httpx.HTTPError as error:
                raise VectorStoreError(
                    "Tidak dapat terhubung ke Qdrant saat mengambil parent context"
                ) from error
            self._raise_for_error(response, "mengambil parent context dari Qdrant")
            try:
                result = response.json()["result"]
                batch = result.get("points", [])
                next_offset = result.get("next_page_offset")
            except (KeyError, TypeError, ValueError) as error:
                raise VectorStoreError(
                    "Respons parent context Qdrant tidak valid"
                ) from error
            points.extend(item for item in batch if isinstance(item, dict))
            if not next_offset or not batch:
                break
            offset = next_offset
        return points[:limit]

    @staticmethod
    def _raise_for_error(response: httpx.Response, operation: str) -> None:
        if response.is_error:
            raise VectorStoreError(f"Gagal {operation} (HTTP {response.status_code})")


async def index_chunks(
    chunks: list[dict[str, Any]],
    embedding_client: Any,
    qdrant_client: QdrantHttpClient,
    *,
    ingestion_id: str,
    batch_size: int,
    on_progress: Callable[[str, int, int], Awaitable[None]] | None = None,
) -> int:
    """Embed and upsert chunks in bounded batches, returning the count stored."""
    if not chunks:
        return 0
    if batch_size <= 0:
        raise VectorStoreError("Ukuran batch indexing harus lebih besar dari 0")

    seen_chunk_ids: set[str] = set()
    duplicate_chunk_ids: set[str] = set()
    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id", "")).strip()
        if not chunk_id:
            raise VectorStoreError("Chunk tanpa chunk_id tidak dapat di-index")
        if chunk_id in seen_chunk_ids:
            duplicate_chunk_ids.add(chunk_id)
        seen_chunk_ids.add(chunk_id)
    if duplicate_chunk_ids:
        examples = ", ".join(sorted(duplicate_chunk_ids)[:3])
        raise VectorStoreError(
            f"Ditemukan {len(duplicate_chunk_ids)} chunk_id duplikat: {examples}"
        )

    indexed = 0
    collection_ready = False
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        texts = [str(chunk.get("text", "")).strip() for chunk in batch]
        if any(not text for text in texts):
            raise VectorStoreError("Chunk kosong tidak dapat di-embed")
        embeddings = await embedding_client.embed(texts)
        batch_end = start + len(batch)
        if on_progress is not None:
            await on_progress("embedding", batch_end, len(chunks))
        if not collection_ready:
            await qdrant_client.ensure_collection(len(embeddings[0].dense))
            collection_ready = True
        await qdrant_client.upsert(batch, embeddings, ingestion_id=ingestion_id)
        indexed += len(batch)
        if on_progress is not None:
            await on_progress("indexing", indexed, len(chunks))
    return indexed
