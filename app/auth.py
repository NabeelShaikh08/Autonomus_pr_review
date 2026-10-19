"""
GitHub App authentication.

Flow:
  1. Sign a short-lived JWT with the app's private key  (proves we are the app)
  2. Exchange it for an installation access token        (proves we own this install)
  3. Use the installation token for all API calls        (expires after 1 hour)
"""

import time
import logging
import httpx
import jwt as pyjwt

from app.config import get_settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def _build_jwt() -> str:
    """Create a 60-second GitHub App JWT signed with our private key."""
    settings = get_settings()
    now = int(time.time())
    payload = {
        "iat": now - 60,   # issued-at skewed back 60s to account for clock drift
        "exp": now + 540,  # expires in 9 min (GitHub max is 10)
        "iss": settings.github_app_id,
    }
    raw = settings.github_private_key.strip()
    # Handle both literal \n (from .env) and real newlines (from Railway)
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    private_key = raw
    return pyjwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    """
    Exchange the app JWT for a per-installation access token.
    This token is used to call the GitHub API on behalf of the installation.
    Tokens are valid for 1 hour; we fetch fresh ones per webhook event.
    """
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

    token = resp.json()["token"]
    logger.debug("Obtained installation token for installation %d", installation_id)
    return token
