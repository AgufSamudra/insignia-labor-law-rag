import asyncio
import json
import logging
from typing import Any, AsyncIterator

from ...models.document_model import DocumentJob


logger = logging.getLogger(__name__)


def make_log_message(
    *,
    job_id: str,
    filename: str,
    stage: str,
    status: str,
    message: str,
) -> str:
    return (
        "Document status | "
        f"job_id={job_id} filename={filename} stage={stage} "
        f"status={status} message={message}"
    )


async def publish_job_event(
    job: DocumentJob,
    *,
    event_type: str,
    filename: str,
    stage: str,
    status: str,
    message: str,
    **extra: Any,
) -> None:
    event = {
        "type": event_type,
        "job_id": job.job_id,
        "filename": filename,
        "stage": stage,
        "status": status,
        "message": message,
        "log": make_log_message(
            job_id=job.job_id,
            filename=filename,
            stage=stage,
            status=status,
            message=message,
        ),
        **extra,
    }
    logger.info(event["log"])
    async with job.lock:
        job.events.append(event)
        subscribers = list(job.subscribers)
        if event_type == "job_status" and status in {"completed", "failed"}:
            job.finished = True
    for subscriber in subscribers:
        subscriber.put_nowait(event)


async def subscribe_to_job(
    job: DocumentJob,
) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async with job.lock:
        history = list(job.events)
        job.subscribers.append(queue)
    return history, queue


async def unsubscribe_from_job(
    job: DocumentJob,
    queue: asyncio.Queue[dict[str, Any]],
) -> None:
    async with job.lock:
        if queue in job.subscribers:
            job.subscribers.remove(queue)


def encode_sse(event: dict[str, Any]) -> str:
    return (
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )


async def stream_job_events(job: DocumentJob) -> AsyncIterator[str]:
    history, queue = await subscribe_to_job(job)
    try:
        for event in history:
            yield encode_sse(event)
        if any(
            event["type"] == "job_status"
            and event["status"] in {"completed", "failed"}
            for event in history
        ):
            return
        while True:
            event = await queue.get()
            yield encode_sse(event)
            if (
                event["type"] == "job_status"
                and event["status"] in {"completed", "failed"}
            ):
                return
    finally:
        await unsubscribe_from_job(job, queue)

