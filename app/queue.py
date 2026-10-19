"""
Redis-backed job queue with webhook deduplication.

Structure:
  LIST  pr_review_queue   — LPUSH to enqueue, BRPOP to dequeue (FIFO)
  SET   pr_delivery_seen  — tracks delivery IDs to skip duplicates
"""

import json
import logging
from app.redis_client import get_redis
from app.config import get_settings

logger = logging.getLogger(__name__)

QUEUE_KEY = "pr_review_queue"
DEDUP_KEY = "pr_delivery_seen"


async def enqueue_review(delivery_id: str, payload: dict) -> bool:
    """
    Push a review job onto the queue.
    Returns True if enqueued, False if this delivery_id was already seen.
    Raises if Redis is unreachable (caller should handle fallback).
    """
    settings = get_settings()
    r = await get_redis()

    # SADD returns 1 if the member is new, 0 if it already existed
    added = await r.sadd(DEDUP_KEY, delivery_id)
    if not added:
        logger.info("Duplicate delivery %s — skipping", delivery_id)
        return False

    await r.expire(DEDUP_KEY, settings.dedup_ttl_seconds)

    job = json.dumps({"delivery_id": delivery_id, "payload": payload})
    await r.lpush(QUEUE_KEY, job)
    logger.info("Enqueued delivery %s", delivery_id)
    return True


async def dequeue_review(timeout: int = 5) -> dict | None:
    """
    Block-pop one job from the queue.
    Returns None on timeout or if queue is empty.
    """
    r = await get_redis()
    result = await r.brpop(QUEUE_KEY, timeout=timeout)
    if result is None:
        return None
    _, raw = result
    return json.loads(raw)


async def queue_depth() -> int:
    """Return current number of jobs waiting in the queue."""
    r = await get_redis()
    return await r.llen(QUEUE_KEY)
