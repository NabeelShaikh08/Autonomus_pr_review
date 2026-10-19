"""
Shared async Redis connection pool.
Falls back gracefully when Redis is unavailable.
"""

import logging
import redis.asyncio as aioredis
from app.config import get_settings

logger = logging.getLogger(__name__)

_pool: aioredis.ConnectionPool | None = None


def get_redis_pool() -> aioredis.ConnectionPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = aioredis.ConnectionPool.from_url(
            settings.redis_url,
            max_connections=10,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _pool


async def get_redis() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=get_redis_pool())
