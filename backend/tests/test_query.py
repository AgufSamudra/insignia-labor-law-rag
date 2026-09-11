import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.core.config import Settings
from src.services.embedding_service import Embedding
from src.services.query_service import (
    DeepInfraQueryClient,
    QueryPipeline,
    RetrievedChunk,
    clean_answer,
    fuse_rrf,
    normalize_query,
    rewrite_query,
    strip_thinking,
)


def make_settings(**overrides) -> Settings:
    values = {
        "embedding_model": "BAAI/bge-m3-multi",
        "deepinfra_api_key": "test-key",
        "deepinfra_base_url": "https://api.deepinfra.com/v1",
        "qdrant_host": "http://qdrant.test",
        "qdrant_api_key": None,
        "qdrant_collection": "test_collection",
        "embedding_batch_size": 2,
        "http_timeout_seconds": 10,
        "rerank_top_k": 7,
        "rerank_min_score": 0.05,
    }
    values.update(overrides)
    return Settings(**values)


class FakeEmbedder:
    async def embed(self, texts):
        self.texts = texts
        return [Embedding([0.1, 0.2], [1, 2], [0.3, 0.4]) for _ in texts]


class FakeQdrant:
    def __init__(self):
        self.search_names = []

    async def search(self, vector_name, vector, *, limit):
        self.search_names.append(vector_name)
        return [
            {
                "id": "point-1",
                "score": 0.9,
                "payload": {
                    "filename": "PP No. 35 Tahun 2021.pdf",
                    "pasal": "8",
                    "ayat": "1",
                    "page_start": 7,
                    "page_end": 7,
                    "chunk_id": "chunk-1",
                    "text": "PKWT berdasarkan jangka waktu dibuat paling lama lima tahun.",
                },
            },
            {
                "id": "point-2",
                "score": 0.7,
                "payload": {
                    "filename": "PP No. 35 Tahun 2021.pdf",
                    "pasal": "8",
                    "ayat": "2",
                    "page_start": 7,
                    "page_end": 7,
                    "chunk_id": "chunk-2",
                    "text": "Ketentuan jangka waktu PKWT.",
                },
            },
        ][:limit]

    async def scroll_parent(self, *, filename, pasal, limit):
        return [
            {
                "id": "point-1",
                "payload": {
                    "filename": filename,
                    "pasal": pasal,
                    "ayat": "1",
                    "chunk_id": "chunk-1",
                    "text": "PKWT berdasarkan jangka waktu dibuat paling lama lima tahun.",
                },
            },
            {
                "id": "point-2",
                "payload": {
                    "filename": filename,
                    "pasal": pasal,
                    "ayat": "2",
                    "chunk_id": "chunk-2",
                    "text": "Ketentuan jangka waktu PKWT.",
                },
            },
        ][:limit]


class FakeModelClient:
    def __init__(self, scores=None):
        self.scores = scores or [0.9, 0.8]
        self.generated_context = ""

    async def rerank(self, _query, chunks):
        for chunk, score in zip(chunks, self.scores):
            chunk.rerank_score = score
        return sorted(chunks, key=lambda item: item.rerank_score, reverse=True)

    async def generate(self, _query, context):
        self.generated_context = context
        return "PKWT paling lama lima tahun [1]."


class TestQueryHelpers(unittest.TestCase):
    def test_normalizes_legal_references(self):
        self.assertEqual(normalize_query("  pasal8   ayat (1)  "), "Pasal 8 Ayat 1")

    def test_every_query_is_rewritten_without_changing_its_intent(self):
        rewritten = rewrite_query("Pasal 8")
        self.assertIn("ketentuan", rewritten)
        long_rewrite = rewrite_query("Berapa lama maksimal PKWT menurut PP 35?")
        self.assertEqual(
            long_rewrite,
            "Berapa lama maksimal PKWT menurut PP 35? "
            "menurut peraturan ketenagakerjaan Indonesia",
        )

    def test_rrf_merges_same_chunk_from_both_searches(self):
        dense = [{"id": "a", "score": 0.9, "payload": {"chunk_id": "a", "text": "A"}}]
        sparse = [{"id": "a", "score": 0.8, "payload": {"chunk_id": "a", "text": "A"}}]
        result = fuse_rrf(dense, sparse, rrf_k=60, limit=20)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].rrf_score, 2 / 61)

    def test_strips_qwen_thinking_block(self):
        answer = "<think>internal reasoning</think>\n\nJawaban [1]."
        self.assertEqual(strip_thinking(answer), "Jawaban [1].")

    def test_cleans_markdown_markers_without_removing_line_breaks(self):
        answer = "**Hak pekerja:**\n1. **Uang pesangon**\n2. Uang penggantian hak"
        self.assertEqual(
            clean_answer(answer),
            "Hak pekerja:\n1. Uang pesangon\n2. Uang penggantian hak",
        )


class TestQueryPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_runs_hybrid_rerank_parent_expansion_and_generation(self):
        embedder = FakeEmbedder()
        qdrant = FakeQdrant()
        model = FakeModelClient()
        result = await QueryPipeline(
            make_settings(), embedder, qdrant, model
        ).run("Berapa lama maksimal PKWT?")

        self.assertEqual(qdrant.search_names, ["dense", "sparse"])
        self.assertIn("ketentuan jangka waktu pkwt", model.generated_context.lower())
        self.assertEqual(result.sources[0]["document"], "PP No. 35 Tahun 2021.pdf")
        self.assertEqual(result.sources[0]["page"], 7)
        self.assertEqual(result.retrieval["reranked_chunks"], 2)
        self.assertEqual(
            result.retrieval["rewritten_query"],
            "Berapa lama maksimal PKWT? menurut peraturan ketenagakerjaan Indonesia",
        )
        self.assertIn("[1]", result.answer)

    async def test_returns_insufficient_evidence_without_generation(self):
        embedder = FakeEmbedder()
        qdrant = FakeQdrant()
        model = FakeModelClient(scores=[0.0, 0.0])
        result = await QueryPipeline(
            make_settings(rerank_min_score=0.1), embedder, qdrant, model
        ).run("pertanyaan")
        self.assertIn("tidak menemukan dasar", result.answer)
        self.assertEqual(result.sources, [])
        self.assertEqual(model.generated_context, "")


class TestDeepInfraReranker(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_reranker_uses_queries_array_and_parses_score_list(self):
        request_body = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_body
            request_body = json.loads(request.content)
            return httpx.Response(200, json=[0.8, 0.2], request=request)

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        query_client = DeepInfraQueryClient(make_settings(), client)
        chunks = [
            RetrievedChunk("1", {"text": "teks satu"}),
            RetrievedChunk("2", {"text": "teks dua"}),
        ]

        ranked = await query_client.rerank("pertanyaan", chunks)

        self.assertEqual(request_body["queries"], ["pertanyaan", "pertanyaan"])
        self.assertEqual(request_body["documents"], ["teks satu", "teks dua"])
        self.assertEqual([chunk.rerank_score for chunk in ranked], [0.8, 0.2])
        await client.aclose()


class TestScopeFilter(unittest.IsolatedAsyncioTestCase):
    async def test_classifies_question_with_json_only_response(self):
        request_body = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_body
            request_body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"is_labor_law": false}'}},
                    ]
                },
                request=request,
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        query_client = DeepInfraQueryClient(make_settings(), client)

        self.assertFalse(await query_client.is_labor_law_question("Cuaca hari ini?"))
        self.assertEqual(request_body["temperature"], 0.0)
        self.assertEqual(request_body["max_tokens"], 16)
        await client.aclose()

    async def test_out_of_scope_query_stops_before_retrieval(self):
        from src.main import query_user
        from src.models.document import QueryRequest

        scope_client = AsyncMock()
        scope_client.is_labor_law_question.return_value = False
        settings = make_settings()

        with (
            patch("src.main.Settings") as settings_class,
            patch("src.main.DeepInfraQueryClient", return_value=scope_client),
            patch("src.main.DeepInfraEmbeddingClient") as embedding_class,
            patch("src.main.QdrantHttpClient") as qdrant_class,
            patch("src.main.QueryPipeline") as pipeline_class,
        ):
            settings_class.from_env.return_value = settings
            result = await query_user(QueryRequest(query="Berapa skor pertandingan?"))

        self.assertTrue(result["retrieval"]["filtered"])
        self.assertEqual(result["sources"], [])
        embedding_class.assert_not_called()
        qdrant_class.assert_not_called()
        pipeline_class.assert_not_called()
        scope_client.aclose.assert_awaited_once()
