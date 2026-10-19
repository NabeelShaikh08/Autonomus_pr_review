"""
GitHub App authentication.

Flow:
  1. Sign a short-lived JWT with the app's private key  (proves we are the app)
  2. Exchange it for an installation access token        (proves we own this install)
  3. Use the installation token for all API calls        (expires after 1 hour)

Tokens are cached per installation and reused until 5 minutes before expiry,
avoiding a round-trip to GitHub on every webhook event.
"""

import time
import logging
import httpx
import jwt as pyjwt

from app.config import get_settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Cache: installation_id → (token, expiry_timestamp)
_token_cache: dict[int, tuple[str, float]] = {}
_TOKEN_EXPIRY_BUFFER = 300  # refresh 5 minutes before actual expiry


def _build_jwt() -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 540,
        "iss": settings.github_app_id,
    }
    raw = settings.github_private_key.strip()
    # Handle both literal \n (from .env) and real newlines (from Railway)
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return pyjwt.encode(payload, raw, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    """
    Return a valid installation access token, using the cache when possible.
    Tokens are valid for 1 hour; we refresh 5 minutes before expiry.
    """
    cached = _token_cache.get(installation_id)
    if cached:
        token, expires_at = cached
        if time.time() < expires_at - _TOKEN_EXPIRY_BUFFER:
            logger.debug("Using cached token for installation %d", installation_id)
            return token

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
        logger.error(
            "Failed to get installation token: %s %s",
            resp.status_code,
            resp.text,
        )
        resp.raise_for_status()

    data = resp.json()
    token = data["token"]

    # GitHub returns expiry as ISO 8601; convert to unix timestamp for comparison
    from datetime import datetime, timezone
    expires_at_str = data.get("expires_at", "")
    try:
        expires_at = datetime.fromisoformat(
            expires_at_str.replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, AttributeError):
        expires_at = time.time() + 3600  # fallback: assume 1 hour

    _token_cache[installation_id] = (token, expires_at)
    logger.debug("Fetched and cached token for installation %d", installation_id)
    return token
