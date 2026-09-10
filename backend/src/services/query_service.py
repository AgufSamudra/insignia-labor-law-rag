"""User-query RAG pipeline: rewrite, hybrid retrieval, reranking, and generation."""

from __future__ import annotations

import logging
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

import httpx

try:
    from ..core.config import Settings
    from ..core.errors import QueryServiceError
    from ..prompt.query_prompt import (
        build_generation_prompts,
        build_scope_classification_prompt,
    )
    from .embedding_service import DeepInfraEmbeddingClient
    from ..repositories.qdrant_repository import QdrantHttpClient
except ImportError:  # Supports running this module directly from src/.
    from core.config import Settings
    from core.errors import QueryServiceError
    from prompt.query_prompt import (
        build_generation_prompts,
        build_scope_classification_prompt,
    )
    from services.embedding_service import DeepInfraEmbeddingClient
    from repositories.qdrant_repository import QdrantHttpClient


logger = logging.getLogger(__name__)
INSUFFICIENT_EVIDENCE = (
    "Saya tidak menemukan dasar yang cukup pada dua dokumen yang tersedia "
    "untuk menjawab pertanyaan tersebut."
)


@dataclass
class RetrievedChunk:
    """A Qdrant payload enriched with retrieval scores."""

    point_id: str
    payload: dict[str, Any]
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float = 0.0

    @property
    def chunk_id(self) -> str:
        return str(self.payload.get("chunk_id") or self.point_id)

    @property
    def text(self) -> str:
        return str(self.payload.get("text", "")).strip()


@dataclass
class QueryResult:
    """Serializable result of the complete query pipeline."""

    answer: str
    sources: list[dict[str, Any]]
    retrieval: dict[str, Any]


def normalize_query(query: str) -> str:
    """Normalize whitespace and legal references without changing user intent."""
    normalized = unicodedata.normalize("NFKC", query).replace("\u00a0", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(
        r"\bpasal\s*([0-9]+[a-z]?)\b",
        r"Pasal \1",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\bayat\s*(?:\(\s*)?([0-9]+)(?:\s*\))?",
        r"Ayat \1",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized


def rewrite_if_ambiguous(query: str, *, enabled: bool = True) -> tuple[str, bool]:
    """Add retrieval context only to very short or underspecified queries."""
    normalized = normalize_query(query)
    if not enabled:
        return normalized, False
    words = re.findall(r"[\wÀ-ÿ]+", normalized, flags=re.UNICODE)
    if len(words) > 4:
        return normalized, False
    if re.search(r"\bPasal\s+\d+|\bAyat\s+\d+", normalized, re.IGNORECASE):
        return f"{normalized} ketentuan dalam peraturan ketenagakerjaan", True
    return f"{normalized} menurut peraturan ketenagakerjaan Indonesia", True


class DeepInfraQueryClient:
    """DeepInfra native reranker and OpenAI-compatible chat client."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds)
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def is_labor_law_question(self, query: str) -> bool:
        """Classify scope before any embedding, retrieval, or reranking work."""
        system_prompt, user_prompt = build_scope_classification_prompt(query)
        logger.info("Scope classification started | model=%s", self.settings.generative_model)
        try:
            response = await self._client.post(
                f"{self.settings.deepinfra_base_url}/openai/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.deepinfra_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.generative_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 16,
                    "stream": False,
                },
            )
            response.raise_for_status()
            body = response.json()
            decision = body["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response)
            raise QueryServiceError(
                f"DeepInfra scope filter gagal (HTTP {error.response.status_code}): {detail}"
            ) from error
        except (httpx.RequestError, KeyError, IndexError, TypeError, ValueError) as error:
            raise QueryServiceError("DeepInfra scope filter gagal") from error

        if not isinstance(decision, str):
            raise QueryServiceError("Respons DeepInfra scope filter tidak valid")
        cleaned_decision = strip_thinking(decision).strip()
        if cleaned_decision.startswith("```"):
            cleaned_decision = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned_decision).strip()
        try:
            parsed = json.loads(cleaned_decision)
        except json.JSONDecodeError:
            json_match = re.search(r"\{.*\}", cleaned_decision, flags=re.DOTALL)
            if not json_match:
                raise QueryServiceError(
                    "Respons DeepInfra scope filter bukan JSON valid"
                )
            try:
                parsed = json.loads(json_match.group(0))
            except json.JSONDecodeError as error:
                raise QueryServiceError(
                    "Respons DeepInfra scope filter bukan JSON valid"
                ) from error
        if not isinstance(parsed, dict) or not isinstance(parsed.get("is_labor_law"), bool):
            raise QueryServiceError("Respons DeepInfra scope filter tidak memiliki keputusan valid")
        logger.info("Scope classification completed | is_labor_law=%s", parsed["is_labor_law"])
        return parsed["is_labor_law"]

    async def rerank(
        self, query: str, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        documents = [chunk.text for chunk in chunks]
        model_name = self.settings.reranked_model.lower()
        if "qwen3-reranker" in model_name:
            # Qwen3-Reranker expects one query per document and returns a
            # score list, unlike the generic DeepInfra reranker contract.
            payload = {
                "queries": [query] * len(documents),
                "documents": documents,
                "instruction": (
                    "Given a legal question, retrieve passages that answer "
                    "the question from Indonesian labor-law regulations"
                ),
            }
        else:
            payload = {"query": query, "documents": documents}
        try:
            response = await self._client.post(
                f"{self.settings.deepinfra_base_url}/inference/"
                f"{self.settings.reranked_model}",
                headers={
                    "Authorization": f"Bearer {self.settings.deepinfra_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            scores = (
                body
                if isinstance(body, list)
                else body.get("scores")
                if isinstance(body, dict)
                else None
            )
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response)
            raise QueryServiceError(
                f"DeepInfra reranker gagal (HTTP {error.response.status_code}): {detail}"
            ) from error
        except (httpx.RequestError, ValueError, TypeError) as error:
            raise QueryServiceError("DeepInfra reranker gagal") from error
        if not isinstance(scores, list) or len(scores) != len(chunks):
            raise QueryServiceError(
                "Respons DeepInfra reranker tidak memiliki scores yang lengkap"
            )
        try:
            for chunk, score in zip(chunks, scores):
                chunk.rerank_score = float(score)
        except (TypeError, ValueError) as error:
            raise QueryServiceError("Score dari DeepInfra reranker tidak valid") from error
        return sorted(
            chunks,
            key=lambda chunk: chunk.rerank_score,
            reverse=True,
        )[: self.settings.rerank_top_k]

    async def generate(self, query: str, context: str) -> str:
        system_prompt, user_prompt = build_generation_prompts(query, context)
        logger.info(
            "Generation request started | model=%s context_chars=%d",
            self.settings.generative_model,
            len(context),
        )
        try:
            response = await self._client.post(
                f"{self.settings.deepinfra_base_url}/openai/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.deepinfra_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.generative_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.7,
                    "max_tokens": self.settings.generation_max_tokens,
                    "stream": False,
                },
            )
            response.raise_for_status()
            body = response.json()
            answer = body["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as error:
            detail = _response_error_detail(error.response)
            raise QueryServiceError(
                f"DeepInfra generative model gagal "
                f"(HTTP {error.response.status_code}): {detail}"
            ) from error
        except (httpx.RequestError, KeyError, IndexError, TypeError, ValueError) as error:
            raise QueryServiceError("DeepInfra generative model gagal") from error
        if not isinstance(answer, str) or not answer.strip():
            raise QueryServiceError("DeepInfra generative model mengembalikan jawaban kosong")
        answer = clean_answer(answer)
        if not answer:
            raise QueryServiceError(
                "DeepInfra generative model hanya mengembalikan reasoning internal"
            )
        return answer


def strip_thinking(answer: str) -> str:
    """Remove Qwen internal reasoning tags before exposing an answer to users."""
    cleaned = re.sub(
        r"<think>.*?(?:</think>|$)",
        "",
        answer,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"</?think>", "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def clean_answer(answer: str) -> str:
    """Remove internal reasoning and presentation markers from model output."""
    cleaned = strip_thinking(answer)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _response_error_detail(response: httpx.Response) -> str:
    """Extract a short provider error without leaking request contents."""
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body
    else:
        detail = body
    return str(detail)[:500]


class QueryPipeline:
    """Orchestrate query preprocessing through a grounded final response."""

    def __init__(
        self,
        settings: Settings,
        embedding_client: DeepInfraEmbeddingClient,
        qdrant_client: QdrantHttpClient,
        model_client: DeepInfraQueryClient,
    ):
        self.settings = settings
        self.embedding_client = embedding_client
        self.qdrant_client = qdrant_client
        self.model_client = model_client

    async def run(self, query: str) -> QueryResult:
        started = time.perf_counter()
        normalized_query = normalize_query(query)
        retrieval_query, was_rewritten = rewrite_if_ambiguous(
            normalized_query,
            enabled=self.settings.query_rewrite_enabled,
        )
        embedding = (await self.embedding_client.embed([retrieval_query]))[0]

        import asyncio

        dense_results, sparse_results = await asyncio.gather(
            self.qdrant_client.search(
                "dense", embedding.dense, limit=self.settings.retrieval_limit
            ),
            self.qdrant_client.search(
                "sparse",
                {
                    "indices": embedding.sparse_indices,
                    "values": embedding.sparse_values,
                },
                limit=self.settings.retrieval_limit,
            ),
        )
        candidates = fuse_rrf(
            dense_results,
            sparse_results,
            rrf_k=self.settings.rrf_k,
            limit=self.settings.retrieval_limit,
        )
        reranked = await self.model_client.rerank(retrieval_query, candidates)
        reranked = [
            chunk
            for chunk in reranked
            if chunk.rerank_score >= self.settings.rerank_min_score
        ]

        if not reranked:
            return QueryResult(
                answer=INSUFFICIENT_EVIDENCE,
                sources=[],
                retrieval={
                    "query": query,
                    "normalized_query": normalized_query,
                    "rewritten_query": retrieval_query if was_rewritten else None,
                    "retrieved_chunks": 0,
                    "candidate_chunks": len(candidates),
                    "reranked_chunks": 0,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )

        context_chunks = await self._expand_parent_context(reranked)
        context = build_context(context_chunks)
        answer = await self.model_client.generate(normalized_query, context)
        sources = [source_for_chunk(chunk, index + 1) for index, chunk in enumerate(reranked)]
        if not re.search(r"\[\d+\]", answer):
            answer = f"{answer}\n\nSumber: " + ", ".join(
                f"[{index}]" for index in range(1, len(sources) + 1)
            )
        return QueryResult(
            answer=answer,
            sources=sources,
            retrieval={
                "query": query,
                "normalized_query": normalized_query,
                "rewritten_query": retrieval_query if was_rewritten else None,
                "retrieved_chunks": len(reranked),
                "candidate_chunks": len(candidates),
                "reranked_chunks": len(reranked),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )

    async def _expand_parent_context(
        self, chunks: list[RetrievedChunk]
    ) -> list[RetrievedChunk]:
        """Expand ayat hits to their Pasal when the current Qdrant client supports it."""
        import asyncio

        parent_loader = getattr(self.qdrant_client, "scroll_parent", None)
        if parent_loader is None:
            return chunks
        parent_keys = list({
            (str(chunk.payload.get("filename", "")), str(chunk.payload.get("pasal", "")))
            for chunk in chunks
            if chunk.payload.get("filename") and chunk.payload.get("pasal")
        })
        if not parent_keys:
            return chunks
        try:
            parent_batches = await asyncio.gather(
                *(
                    parent_loader(filename=filename, pasal=pasal, limit=64)
                    for filename, pasal in parent_keys
                )
            )
        except Exception:
            logger.warning("Parent context expansion gagal; memakai chunk hasil rerank", exc_info=True)
            return chunks

        expanded: list[RetrievedChunk] = []
        parent_by_key = dict(zip(parent_keys, parent_batches))
        expanded_parents: set[tuple[str, str]] = set()
        for chunk in chunks:
            key = (
                str(chunk.payload.get("filename", "")),
                str(chunk.payload.get("pasal", "")),
            )
            batch = parent_by_key.get(key, [])
            if key not in expanded_parents and batch:
                parent_text = "\n".join(
                    str(item.get("payload", {}).get("text", "")).strip()
                    for item in sorted(
                        batch,
                        key=lambda value: str(
                            value.get("payload", {}).get("chunk_id", "")
                        ),
                    )
                    if item.get("payload", {}).get("text")
                )
                # Keep expansion useful without allowing one very long Pasal to
                # crowd all other retrieved evidence out of the prompt.
                chunk.payload = dict(chunk.payload)
                chunk.payload["_context_text"] = parent_text[:6000]
                expanded_parents.add(key)
            expanded.append(chunk)
        return expanded


def fuse_rrf(
    dense_results: list[dict[str, Any]],
    sparse_results: list[dict[str, Any]],
    *,
    rrf_k: int = 60,
    limit: int = 20,
) -> list[RetrievedChunk]:
    """Fuse dense and sparse result lists with Reciprocal Rank Fusion."""
    by_id: dict[str, RetrievedChunk] = {}
    for result_list, score_name in (
        (dense_results, "dense_score"),
        (sparse_results, "sparse_score"),
    ):
        for rank, result in enumerate(result_list, start=1):
            payload = result.get("payload") or {}
            point_id = str(result.get("id", ""))
            chunk_id = str(payload.get("chunk_id") or point_id)
            if not chunk_id:
                continue
            chunk = by_id.setdefault(
                chunk_id,
                RetrievedChunk(point_id=point_id, payload=dict(payload)),
            )
            setattr(chunk, score_name, float(result.get("score", 0.0)))
            chunk.rrf_score += 1 / (rrf_k + rank)
    return sorted(by_id.values(), key=lambda chunk: chunk.rrf_score, reverse=True)[:limit]


def build_context(chunks: list[RetrievedChunk]) -> str:
    """Build a labeled context so generated citations can be checked by callers."""
    sections: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        payload = chunk.payload
        page_start = payload.get("page_start", payload.get("page"))
        page_end = payload.get("page_end", page_start)
        page = str(page_start) if page_start == page_end else f"{page_start}-{page_end}"
        article = f"Pasal {payload['pasal']}" if payload.get("pasal") else ""
        paragraph = f"Ayat {payload['ayat']}" if payload.get("ayat") else ""
        metadata = " · ".join(
            item for item in [str(payload.get("filename", "")), f"halaman {page}", article, paragraph] if item
        )
        context_text = str(payload.get("_context_text") or chunk.text)
        sections.append(f"[{index}] {metadata}\n{context_text}")
    return "\n\n".join(sections)


def source_for_chunk(chunk: RetrievedChunk, source_id: int) -> dict[str, Any]:
    """Return the stable citation shape exposed by POST /v1/query."""
    payload = chunk.payload
    source: dict[str, Any] = {
        "id": source_id,
        "document": payload.get("filename", ""),
        "page": payload.get("page_start", payload.get("page")),
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "rerank_score": round(chunk.rerank_score, 6),
    }
    if payload.get("page_end") is not None and payload.get("page_end") != source["page"]:
        source["page_end"] = payload["page_end"]
    if payload.get("bab"):
        source["chapter"] = payload["bab"]
    if payload.get("pasal") is not None:
        source["article"] = f"Pasal {payload['pasal']}"
    if payload.get("ayat") is not None:
        source["paragraph"] = f"Ayat {payload['ayat']}"
    return source
