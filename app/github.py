"""
All GitHub REST API interactions.

Handles:
  - Fetching PR metadata and changed files
  - Posting inline review comments (batched into a single review)
  - Rate limit detection and back-off
  - Retries on transient 5xx errors
"""

import asyncio
import logging
import time
from typing import Any

import httpx

from app.metrics import github_errors_total

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2  # seconds


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get(client: httpx.AsyncClient, url: str, token: str, **kwargs) -> Any:
    """GET with retry + rate-limit handling."""
    for attempt in range(1, _MAX_RETRIES + 1):
        resp = await client.get(url, headers=_headers(token), timeout=20, **kwargs)

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = int(resp.headers.get("X-RateLimit-Reset", 0))
            wait = max(reset - int(time.time()), 1)
            logger.warning("GitHub rate limit hit; waiting %ds", wait)
            await asyncio.sleep(min(wait, 60))
            continue

        if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_BACKOFF * attempt)
            continue

        logger.error("GitHub GET %s → %d: %s", url, resp.status_code, resp.text[:300])
        github_errors_total.labels(status_code=str(resp.status_code)).inc()
        resp.raise_for_status()

    raise RuntimeError(f"GitHub GET {url} failed after {_MAX_RETRIES} attempts")


async def get_pr_files(
    owner: str, repo: str, pr_number: int, token: str
) -> list[dict]:
    """
    Return the list of files changed in a PR.
    Each item has: filename, status, patch (may be None for binaries).
    GitHub paginates at 30 files; we fetch up to page 10 (300 files).
    """
    files: list[dict] = []
    async with httpx.AsyncClient() as client:
        for page in range(1, 11):
            url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files"
            page_data = await _get(
                client, url, token, params={"per_page": 30, "page": page}
            )
            if not page_data:
                break
            files.extend(page_data)
            if len(page_data) < 30:
                break

    logger.info("Fetched %d changed files for %s/%s #%d", len(files), owner, repo, pr_number)
    return files


async def get_pr_details(
    owner: str, repo: str, pr_number: int, token: str
) -> dict:
    """Return PR metadata (title, body, head SHA, base branch)."""
    async with httpx.AsyncClient() as client:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
        return await _get(client, url, token)


async def post_review(
    owner: str,
    repo: str,
    pr_number: int,
    commit_sha: str,
    token: str,
    summary: str,
    comments: list[dict],
) -> None:
    """
    Post a GitHub pull request review.

    Batches all inline comments into a single API call (much more efficient
    than one call per comment, and avoids notification spam).

    comments: list of dicts with keys: path, line, body
    """
    # GitHub only accepts comments on lines present in the diff.
    # Invalid line numbers cause the whole review to fail, so we
    # post invalid ones as top-level review body text instead.
    valid_comments = []
    overflow_notes: list[str] = []

    for c in comments:
        if c.get("line") and c.get("path"):
            valid_comments.append({
                "path": c["path"],
                "line": c["line"],
                "side": "RIGHT",
                "body": c["body"],
            })
        else:
            overflow_notes.append(f"**{c.get('path', 'general')}**: {c['body']}")

    full_body = summary
    if overflow_notes:
        full_body += "\n\n---\n**Additional notes:**\n" + "\n".join(
            f"- {n}" for n in overflow_notes
        )

    payload = {
        "commit_id": commit_sha,
        "body": full_body,
        "event": "COMMENT",
        "comments": valid_comments,
    }

    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

    for attempt in range(1, _MAX_RETRIES + 1):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=_headers(token),
                json=payload,
                timeout=20,
            )

        if resp.status_code in (200, 201):
            logger.info(
                "Posted review on %s/%s #%d (%d inline comments)",
                owner, repo, pr_number, len(valid_comments),
            )
            return

        if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
            await asyncio.sleep(_RETRY_BACKOFF * attempt)
            continue

        logger.error(
            "Failed to post review: %d %s", resp.status_code, resp.text[:300]
        )
        github_errors_total.labels(status_code=str(resp.status_code)).inc()
        resp.raise_for_status()
