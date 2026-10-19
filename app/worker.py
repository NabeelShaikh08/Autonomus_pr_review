"""
Standalone async worker process.

Pulls jobs from the Redis queue and runs PR reviews concurrently.
Deploy as a separate Railway service with start command: python -m app.worker

Graceful shutdown: on SIGTERM/SIGINT, stops accepting new jobs and
waits for all in-flight reviews to complete before exiting.
"""

import asyncio
import logging
import signal

from app.logging_config import configure_logging, set_trace_id
from app.config import get_settings
from app.queue import dequeue_review

logger = logging.getLogger(__name__)

_shutdown = asyncio.Event()


def _handle_signal(sig, frame):
    logger.info("Received signal %s — initiating graceful shutdown", sig)
    _shutdown.set()


async def run_worker(concurrency: int = 3) -> None:
    # Import here to avoid circular imports at module level
    from app.main import _handle_pr_event

    semaphore = asyncio.Semaphore(concurrency)
    in_flight: set[asyncio.Task] = set()

    async def process_one(job: dict) -> None:
        async with semaphore:
            delivery_id = job.get("delivery_id", "-")
            set_trace_id(delivery_id)
            logger.info("Processing delivery %s", delivery_id)
            await _handle_pr_event(job["payload"], delivery_id)

    logger.info("Worker started (concurrency=%d)", concurrency)

    while not _shutdown.is_set():
        job = await dequeue_review(timeout=2)
        if job is None:
            continue
        task = asyncio.create_task(process_one(job))
        in_flight.add(task)
        task.add_done_callback(in_flight.discard)

    # Drain in-flight tasks before exiting
    if in_flight:
        logger.info("Draining %d in-flight tasks before shutdown", len(in_flight))
        await asyncio.gather(*in_flight, return_exceptions=True)

    logger.info("Worker shutdown complete")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    asyncio.run(run_worker(concurrency=settings.worker_concurrency))


if __name__ == "__main__":
    main()
