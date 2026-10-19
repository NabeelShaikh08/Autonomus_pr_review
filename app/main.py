"""
FastAPI webhook server.

POST /webhook  — receives GitHub webhooks, enqueues reviews via Redis
GET  /health   — checks GitHub + Groq reachability
GET  /metrics  — Prometheus metrics
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time as _time
import uuid

from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.agent import run_review
from app.auth import get_installation_token
from app.config import get_settings
from app.filters import build_diff_text, filter_files
from app.github import get_pr_details, get_pr_files, post_review
from app.logging_config import configure_logging, get_trace_id, set_trace_id
from app.queue import enqueue_review

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    if not settings.is_configured():
        raise RuntimeError(
            "Missing required environment variables. "
            "Copy .env.example to .env and fill in all values."
        )

    logger.info("PR Reviewer starting up (App ID: %s)", settings.github_app_id)

    # Init database if configured
    from app.db import init_db, close_db
    await init_db()

    # Start queue depth poller
    poller_task = asyncio.create_task(_poll_queue_depth())

    yield

    poller_task.cancel()
    try:
        await poller_task
    except asyncio.CancelledError:
        pass

    await close_db()
    logger.info("PR Reviewer shutting down")


app = FastAPI(
    title="PR Reviewer",
    description="Autonomous PR code reviewer",
    version="2.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_signature(payload: bytes, signature_header: str | None) -> None:
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256")
    secret = get_settings().github_webhook_secret.encode()
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


# ---------------------------------------------------------------------------
# Verdict extraction
# ---------------------------------------------------------------------------

_VERDICT_RE = re.compile(r"\b(APPROVED|NEEDS_WORK|CRITICAL)\b")

def _extract_verdict(summary: str) -> str | None:
    m = _VERDICT_RE.search(summary)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Core review orchestration
# ---------------------------------------------------------------------------

async def _handle_pr_event(payload: dict, trace_id: str = "-") -> None:
    """
    Full review pipeline. Called by the worker (or BackgroundTasks in local mode).
    All exceptions are caught, logged, and written to the audit log.
    """
    set_trace_id(trace_id)
    start = _time.monotonic()
    owner = repo_name = ""
    pr_number = 0
    verdict: str | None = None
    comment_count = 0
    error_msg: str | None = None

    try:
        pr = payload["pull_request"]
        repo = payload["repository"]
        installation_id: int = payload["installation"]["id"]
        owner = repo["owner"]["login"]
        repo_name = repo["name"]
        pr_number = pr["number"]
        commit_sha: str = pr["head"]["sha"]
        pr_title: str = pr["title"]
        pr_body: str = pr.get("body") or ""
    except KeyError as e:
        logger.error("Malformed webhook payload — missing key: %s", e)
        return

    logger.info(
        "Processing PR #%d in %s/%s (commit %s)",
        pr_number, owner, repo_name, commit_sha[:7],
    )

    try:
        token = await get_installation_token(installation_id)
        raw_files = await get_pr_files(owner, repo_name, pr_number, token)
        reviewable = filter_files(raw_files)

        if not reviewable:
            logger.info("PR #%d has no reviewable files — skipping", pr_number)
            verdict = "SKIPPED"
            return

        logger.info(
            "Reviewing %d/%d files for PR #%d",
            len(reviewable), len(raw_files), pr_number,
        )

        diff_text = build_diff_text(reviewable)
        summary, comments = await run_review(pr_title, pr_body, diff_text)
        verdict = _extract_verdict(summary)
        comment_count = len(comments)

        await post_review(
            owner=owner,
            repo=repo_name,
            pr_number=pr_number,
            commit_sha=commit_sha,
            token=token,
            summary=summary,
            comments=comments,
        )

        logger.info(
            "Review posted for %s/%s #%d — verdict=%s comments=%d",
            owner, repo_name, pr_number, verdict, comment_count,
        )

        # Metrics
        from app.metrics import reviews_total, review_latency_seconds
        reviews_total.labels(verdict=verdict or "UNKNOWN").inc()
        review_latency_seconds.observe(_time.monotonic() - start)

    except Exception as exc:
        error_msg = str(exc)
        logger.exception(
            "Unhandled error while reviewing %s/%s #%d",
            owner, repo_name, pr_number,
        )
        from app.metrics import reviews_total
        reviews_total.labels(verdict="ERROR").inc()

    finally:
        from app.db import log_review
        latency_ms = int((_time.monotonic() - start) * 1000)
        await log_review(
            delivery_id=trace_id,
            repo_owner=owner,
            repo_name=repo_name,
            pr_number=pr_number,
            verdict=verdict,
            comment_count=comment_count,
            latency_ms=latency_ms,
            error=error_msg,
        )


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
) -> JSONResponse:
    payload_bytes = await request.body()

    # Security: verify before parsing
    _verify_signature(payload_bytes, x_hub_signature_256)

    # Track all received webhooks
    from app.metrics import webhooks_received_total
    action_label = ""

    if x_github_event != "pull_request":
        logger.debug("Ignoring event: %s", x_github_event)
        webhooks_received_total.labels(event=x_github_event or "", action="").inc()
        return JSONResponse({"status": "ignored", "event": x_github_event}, status_code=202)

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action: str = payload.get("action", "")
    webhooks_received_total.labels(event="pull_request", action=action).inc()

    if action not in {"opened", "synchronize", "reopened"}:
        logger.debug("Ignoring pull_request action: %s", action)
        return JSONResponse({"status": "ignored", "action": action}, status_code=202)

    if "installation" not in payload:
        logger.error("No installation in payload — is this a GitHub App webhook?")
        raise HTTPException(
            status_code=400,
            detail="Missing installation field. Configure this webhook via a GitHub App.",
        )

    delivery_id = x_github_delivery or str(uuid.uuid4())
    set_trace_id(delivery_id)

    # Enqueue via Redis if available, else run in-process (local/test mode)
    try:
        enqueued = await enqueue_review(delivery_id, payload)
        if not enqueued:
            from app.metrics import duplicates_skipped_total
            duplicates_skipped_total.inc()
            return JSONResponse(
                {"status": "duplicate", "delivery_id": delivery_id},
                status_code=202,
            )
    except Exception:
        # Redis unavailable — fall back to in-process background task
        logger.warning("Redis unavailable — falling back to in-process review")
        import asyncio as _asyncio
        _asyncio.create_task(_handle_pr_event(payload, delivery_id))

    return JSONResponse({"status": "accepted", "delivery_id": delivery_id}, status_code=202)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    import httpx
    checks: dict[str, str] = {}

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get("https://api.github.com", timeout=5)
        checks["github"] = "ok" if r.status_code in (200, 301) else "degraded"
    except Exception:
        checks["github"] = "unreachable"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get("https://api.groq.com", timeout=5)
        checks["groq"] = "ok" if r.status_code < 500 else "degraded"
    except Exception:
        checks["groq"] = "unreachable"

    from app.queue import queue_depth
    from app.redis_client import get_redis
    try:
        r = await get_redis()
        await r.ping()
        depth = await queue_depth()
        checks["redis"] = "ok"
        checks["queue_depth"] = str(depth)
    except Exception:
        checks["redis"] = "unreachable"
        checks["queue_depth"] = "unavailable"

    from app.db import _pool as db_pool
    try:
        if db_pool is None:
            checks["postgres"] = "not_configured"
        else:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT COUNT(*) AS total FROM reviews")
                checks["postgres"] = "ok"
                checks["total_reviews"] = str(row["total"])
    except Exception:
        checks["postgres"] = "unreachable"

    overall = "ok" if all(
        v in ("ok", "not_configured") for k, v in checks.items()
        if k in ("github", "groq", "redis", "postgres")
    ) else "degraded"
    return JSONResponse({"status": overall, "checks": checks})


# ---------------------------------------------------------------------------
# Metrics endpoint
# ---------------------------------------------------------------------------

@app.get("/metrics")
async def metrics() -> Response:
    from app.metrics import generate_latest, CONTENT_TYPE_LATEST
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Background queue depth poller
# ---------------------------------------------------------------------------

async def _poll_queue_depth() -> None:
    from app.metrics import queue_depth_gauge
    from app.queue import queue_depth
    while True:
        try:
            depth = await queue_depth()
            queue_depth_gauge.set(depth)
        except Exception:
            pass
        await asyncio.sleep(15)
