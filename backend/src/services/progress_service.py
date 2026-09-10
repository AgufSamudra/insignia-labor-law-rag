"""In-memory document jobs and Server-Sent Events progress streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from uuid import uuid4


logger = logging.getLogger(__name__)

STAGE_MESSAGES = {
    "upload": "File diterima dan menunggu proses",
    "parsing": "Membaca PDF dan menjalankan OCR bila diperlukan",
    "embedding": "Membuat dense + sparse embedding di DeepInfra",
    "indexing": "Menyimpan vector dan metadata ke Qdrant",
    "completed": "Dokumen selesai diproses",
    "failed": "Pemrosesan dokumen gagal",
}

TERMINAL_STATUSES = {"completed", "failed"}


def make_log_message(
    *,
    job_id: str,
    filename: str,
    stage: str,
    status: str,
    message: str,
) -> str:
    """Create the exact status message written by BE and echoed by FE."""
    return (
        "Document status | "
        f"job_id={job_id} filename={filename} stage={stage} "
        f"status={status} message={message}"
    )


@dataclass
class DocumentJob:
    """Track one upload request and broadcast its events to SSE clients."""

    job_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    finished: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def publish(
        self,
        *,
        event_type: str,
        filename: str,
        stage: str,
        status: str,
        message: str,
        **extra: Any,
    ) -> None:
        """Store an event, log it, and send it to connected SSE clients."""
        event = {
            "type": event_type,
            "job_id": self.job_id,
            "filename": filename,
            "stage": stage,
            "status": status,
            "message": message,
            "log": make_log_message(
                job_id=self.job_id,
                filename=filename,
                stage=stage,
                status=status,
                message=message,
            ),
            **extra,
        }
        logger.info(event["log"])

        async with self.lock:
            self.events.append(event)
            subscribers = list(self.subscribers)
            if event_type == "job_status" and status in TERMINAL_STATUSES:
                self.finished = True

        for subscriber in subscribers:
            subscriber.put_nowait(event)

    async def subscribe(self) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]:
        """Register a subscriber and return events that happened before it connected."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self.lock:
            history = list(self.events)
            self.subscribers.append(queue)
        return history, queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self.lock:
            if queue in self.subscribers:
                self.subscribers.remove(queue)


JOBS: dict[str, DocumentJob] = {}


def create_job() -> DocumentJob:
    """Create and register an in-memory job."""
    job = DocumentJob(job_id=uuid4().hex)
    JOBS[job.job_id] = job
    return job


def get_job(job_id: str) -> DocumentJob | None:
    return JOBS.get(job_id)


def encode_sse(event: dict[str, Any]) -> str:
    """Encode one JSON event using the SSE wire format."""
    return (
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )


async def stream_job_events(job: DocumentJob) -> AsyncIterator[str]:
    """Yield job history and then wait for new progress events."""
    history, queue = await job.subscribe()
    try:
        for event in history:
            yield encode_sse(event)

        if any(
            event["type"] == "job_status"
            and event["status"] in TERMINAL_STATUSES
            for event in history
        ):
            return

        while True:
            event = await queue.get()
            yield encode_sse(event)
            if (
                event["type"] == "job_status"
                and event["status"] in TERMINAL_STATUSES
            ):
                return
    finally:
        await job.unsubscribe(queue)
