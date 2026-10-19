"""
GitHub App authentication.

Flow:
  1. Sign a short-lived JWT with the app's private key  (proves we are the app)
  2. Exchange it for an installation access token        (proves we own this install)
  3. Use the installation token for all API calls        (expires after 1 hour)

Tokens are cached in Redis (shared across workers) with a TTL, falling back to
in-process memory if Redis is unavailable. A per-installation asyncio lock
prevents duplicate fetches within a single process.
"""

import asyncio
import time
import logging
from datetime import datetime
import httpx
import jwt as pyjwt

from app.config import get_settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# JWT timing constants (GitHub requirements)
_JWT_ISS_OFFSET = -60    # skew back 60s to account for clock drift
_JWT_EXP_DURATION = 540  # 9 minutes (GitHub max is 10)

# In-process fallback cache: installation_id → (token, expiry_timestamp)
_local_cache: dict[int, tuple[str, float]] = {}
_token_locks: dict[int, asyncio.Lock] = {}
_TOKEN_EXPIRY_BUFFER = 300  # refresh 5 minutes before actual expiry

_REDIS_KEY_PREFIX = "gh_token:"


def _build_jwt() -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iat": now + _JWT_ISS_OFFSET,
        "exp": now + _JWT_EXP_DURATION,
        "iss": settings.github_app_id,
    }
    raw = settings.github_private_key.strip()
    # Handle both literal \n (from .env) and real newlines (from Railway)
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return pyjwt.encode(payload, raw, algorithm="RS256")


async def _get_from_redis(installation_id: int) -> str | None:
    """Return cached token from Redis, or None if missing/unavailable."""
    try:
        from app.redis_client import get_redis
        r = await get_redis()
        value = await r.get(f"{_REDIS_KEY_PREFIX}{installation_id}")
        return value  # None if key missing
    except Exception:
        return None


async def _set_in_redis(installation_id: int, token: str, expires_at: float) -> None:
    """Store token in Redis with TTL aligned to expiry (minus buffer)."""
    try:
        from app.redis_client import get_redis
        r = await get_redis()
        ttl = max(int(expires_at - time.time() - _TOKEN_EXPIRY_BUFFER), 60)
        await r.setex(f"{_REDIS_KEY_PREFIX}{installation_id}", ttl, token)
    except Exception:
        pass  # Redis unavailable — in-process cache is the fallback


async def get_installation_token(installation_id: int) -> str:
    """
    Return a valid installation access token.

    Lookup order:
      1. In-process cache (fastest — no network)
      2. Redis cache (shared across all worker instances)
      3. GitHub API (fetches fresh token, writes to both caches)

    A per-installation asyncio lock prevents duplicate fetches within one process.
    """
    # 1. In-process cache (fast path — no lock needed for reads)
    cached = _local_cache.get(installation_id)
    if cached:
        token, expires_at = cached
        if time.time() < expires_at - _TOKEN_EXPIRY_BUFFER:
            logger.debug("Using in-process cached token for installation %d", installation_id)
            return token

    # Acquire per-installation lock to serialise fetches within this process
    lock = _token_locks.setdefault(installation_id, asyncio.Lock())

    async with lock:
        # Re-check in-process cache (another coroutine may have fetched already)
        cached = _local_cache.get(installation_id)
        if cached:
            token, expires_at = cached
            if time.time() < expires_at - _TOKEN_EXPIRY_BUFFER:
                logger.debug("Using in-process cached token for installation %d (post-lock)", installation_id)
                return token

        # 2. Redis cache (shared across worker instances)
        token = await _get_from_redis(installation_id)
        if token:
            # Store in local cache with a conservative 55-min expiry
            # (Redis TTL already accounts for the buffer)
            expires_at = time.time() + 3300
            _local_cache[installation_id] = (token, expires_at)
            logger.debug("Using Redis cached token for installation %d", installation_id)
            return token

        # 3. Fetch from GitHub API
        jwt_token = _build_jwt()
        url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, timeout=15)

        if resp.status_code != 201:
            logger.error("Failed to get installation token: %d", resp.status_code)
            resp.raise_for_status()

        data = resp.json()
        token = data["token"]
        expires_at_str = data.get("expires_at", "")
        try:
            expires_at = datetime.fromisoformat(
                expires_at_str.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            expires_at = time.time() + 3600

        _local_cache[installation_id] = (token, expires_at)
        await _set_in_redis(installation_id, token, expires_at)
        logger.debug("Fetched and cached token for installation %d", installation_id)
        return token
