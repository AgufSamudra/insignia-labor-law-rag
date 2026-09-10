import unittest

from src.services.progress_service import DocumentJob, stream_job_events


class TestDocumentProgress(unittest.IsolatedAsyncioTestCase):
    async def test_stream_replays_history_and_finishes_after_job_event(self):
        job = DocumentJob(job_id="job-1")
        await job.publish(
            event_type="document_status",
            filename="aturan.pdf",
            stage="upload",
            status="queued",
            message="File diterima dan menunggu proses",
        )

        stream = stream_job_events(job)
        first_event = await stream.__anext__()
        self.assertIn('event: document_status', first_event)
        self.assertIn('"stage": "upload"', first_event)
        self.assertIn(
            "Document status | job_id=job-1 filename=aturan.pdf stage=upload",
            first_event,
        )

        await job.publish(
            event_type="job_status",
            filename="-",
            stage="job",
            status="completed",
            message="Pemrosesan upload selesai",
        )
        terminal_event = await stream.__anext__()
        self.assertIn('event: job_status', terminal_event)
        self.assertIn('"status": "completed"', terminal_event)
        with self.assertRaises(StopAsyncIteration):
            await stream.__anext__()
