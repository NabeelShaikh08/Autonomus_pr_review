"""
PostgreSQL audit log for every completed review.

Table is created automatically on startup if DATABASE_URL is set.
All operations are best-effort — a DB failure never breaks a review.
"""

import logging
import asyncpg
from app.config import get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS reviews (
    id            BIGSERIAL    PRIMARY KEY,
    delivery_id   TEXT         NOT NULL,
    repo_owner    TEXT         NOT NULL,
    repo_name     TEXT         NOT NULL,
    pr_number     INTEGER      NOT NULL,
    verdict       TEXT,
    comment_count INTEGER,
    latency_ms    INTEGER,
    error         TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS reviews_repo_pr
    ON reviews (repo_owner, repo_name, pr_number);
"""


async def init_db() -> None:
    global _pool
    settings = get_settings()
    if not settings.database_url:
        logger.info("DATABASE_URL not set — audit log disabled")
        return
    try:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
        async with _pool.acquire() as conn:
            await conn.execute(_CREATE_SQL)
        logger.info("Database pool ready")
    except Exception:
        logger.exception("Failed to initialise database — audit log disabled")
        _pool = None


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def log_review(
    *,
    delivery_id: str,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    verdict: str | None,
    comment_count: int,
    latency_ms: int,
    error: str | None = None,
) -> None:
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO reviews
                  (delivery_id, repo_owner, repo_name, pr_number,
                   verdict, comment_count, latency_ms, error)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                delivery_id, repo_owner, repo_name, pr_number,
                verdict, comment_count, latency_ms, error,
            )
    except Exception:
        logger.exception("Failed to write audit log row — review still succeeded")
