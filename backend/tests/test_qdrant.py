import json
import unittest

import httpx

from src.core.config import Settings
from src.core.errors import VectorStoreError
from src.repositories.qdrant_repository import QdrantHttpClient, index_chunks
from src.services.embedding_service import Embedding


def make_settings() -> Settings:
    return Settings(
        embedding_model="BAAI/bge-m3-multi",
        deepinfra_api_key="test-key",
        deepinfra_base_url="https://api.deepinfra.com/v1",
        qdrant_host="http://qdrant.test",
        qdrant_api_key=None,
        qdrant_collection="test_collection",
        embedding_batch_size=2,
        http_timeout_seconds=10,
    )


class TestQdrantIndexing(unittest.IsolatedAsyncioTestCase):
    async def test_creates_hybrid_collection_and_upserts(self):
        requests: list[httpx.Request] = []
        progress_events: list[tuple[str, int, int]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(404, request=request)
            return httpx.Response(200, json={"result": {"status": "ok"}}, request=request)

        transport = httpx.MockTransport(handler)
        qdrant = QdrantHttpClient(make_settings(), httpx.AsyncClient(transport=transport))

        class FakeEmbedder:
            async def embed(self, texts: list[str]) -> list[Embedding]:
                return [Embedding([0.1, 0.2], [1], [0.5]) for _ in texts]

        async def record_progress(stage: str, current: int, total: int) -> None:
            progress_events.append((stage, current, total))

        count = await index_chunks(
            [{"chunk_id": "doc-pasal1-part1", "text": "teks hukum"}],
            FakeEmbedder(),
            qdrant,
            ingestion_id="ingestion-1",
            batch_size=2,
            on_progress=record_progress,
        )

        self.assertEqual(count, 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[1].method, "PUT")
        create_body = json.loads(requests[1].content)
        self.assertEqual(
            create_body["vectors"]["dense"], {"size": 2, "distance": "Cosine"}
        )
        upsert_body = json.loads(requests[2].content)
        point = upsert_body["points"][0]
        self.assertEqual(point["vector"]["sparse"], {"indices": [1], "values": [0.5]})
        self.assertEqual(point["payload"]["chunk_id"], "doc-pasal1-part1")
        self.assertEqual(
            progress_events,
            [("embedding", 1, 1), ("indexing", 1, 1)],
        )
        await qdrant._client.aclose()

    async def test_rejects_duplicate_chunk_ids_before_embedding(self):
        class FakeEmbedder:
            async def embed(self, _texts: list[str]) -> list[Embedding]:
                raise AssertionError("Embedding tidak boleh dijalankan")

        class FakeQdrant:
            pass

        chunks = [
            {"chunk_id": "duplicate", "text": "teks pertama"},
            {"chunk_id": "duplicate", "text": "teks kedua"},
        ]

        with self.assertRaisesRegex(VectorStoreError, "chunk_id duplikat"):
            await index_chunks(
                chunks,
                FakeEmbedder(),
                FakeQdrant(),
                ingestion_id="ingestion-1",
                batch_size=2,
            )
