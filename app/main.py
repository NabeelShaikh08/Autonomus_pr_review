"""
FastAPI webhook server.

Single endpoint: POST /webhook
  - Validates GitHub's HMAC-SHA256 signature
  - Accepts pull_request events (opened, synchronize, reopened)
  - Acknowledges immediately (202) then processes in background
  - Orchestrates: auth → fetch files → filter → review → post
"""

import asyncio
import hashlib
import hmac
import logging
import logging.config

from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from app.agent import run_review
from app.auth import get_installation_token
from app.config import get_settings
from app.filters import build_diff_text, filter_files
from app.github import get_pr_details, get_pr_files, post_review

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            }
        },
        # Read log level from env directly to avoid triggering Settings validation
        # before the app is fully initialised (e.g. during test collection).
        "root": {
            "level": __import__("os").environ.get("LOG_LEVEL", "INFO"),
            "handlers": ["console"],
        },
    }
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.is_configured():
        raise RuntimeError(
            "Missing required environment variables. "
            "Copy .env.example to .env and fill in all values."
        )
    logger.info("PR Reviewer starting up (App ID: %s)", settings.github_app_id)
    yield
    logger.info("PR Reviewer shutting down")


app = FastAPI(
    title="PR Reviewer",
    description="Autonomous PR code reviewer powered by Claude",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_signature(payload: bytes, signature_header: str | None) -> None:
    """
    Verify the X-Hub-Signature-256 header GitHub sends with every webhook.
    Raises HTTP 401 if missing or invalid.
    This MUST run before any payload processing.
    """
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256")

    secret = get_settings().github_webhook_secret.encode()
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


# ---------------------------------------------------------------------------
# Core review orchestration
# ---------------------------------------------------------------------------

async def _handle_pr_event(payload: dict) -> None:
    """
    Full review pipeline. Runs in the background after webhook is acknowledged.
    Any exception here is logged but does not affect the HTTP response.
    """
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation_id: int = payload["installation"]["id"]

    owner: str = repo["owner"]["login"]
    repo_name: str = repo["name"]
    pr_number: int = pr["number"]
    commit_sha: str = pr["head"]["sha"]
    pr_title: str = pr["title"]
    pr_body: str = pr.get("body") or ""

    logger.info(
        "Processing PR #%d in %s/%s (commit %s)",
        pr_number, owner, repo_name, commit_sha[:7],
    )

    try:
        # 1. Get a short-lived installation token
        token = await get_installation_token(installation_id)

        # 2. Fetch changed files (paginated)
        raw_files = await get_pr_files(owner, repo_name, pr_number, token)

        # 3. Filter out generated/vendor/irrelevant files
        reviewable = filter_files(raw_files)

        if not reviewable:
            logger.info(
                "PR #%d has no reviewable files after filtering — skipping",
                pr_number,
            )
            return

        logger.info(
            "Reviewing %d/%d files for PR #%d",
            len(reviewable), len(raw_files), pr_number,
        )

        # 4. Build a single diff text string for Claude
        diff_text = build_diff_text(reviewable)

        # 5. Run the Claude agentic review loop
        summary, comments = await run_review(pr_title, pr_body, diff_text)

        # 6. Post the review back to GitHub
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
            "Review posted for %s/%s #%d — %d comments",
            owner, repo_name, pr_number, len(comments),
        )

    except Exception:
        logger.exception(
            "Unhandled error while reviewing %s/%s #%d",
            owner, repo_name, pr_number,
        )


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> JSONResponse:
    """
    Receive a GitHub webhook event.

    We verify the signature synchronously, then acknowledge immediately
    with 202 Accepted. The actual review runs in the background so we
    don't block GitHub's delivery timeout (10 seconds).
    """
    payload_bytes = await request.body()

    # Security: verify before parsing
    _verify_signature(payload_bytes, x_hub_signature_256)

    # Only process pull_request events
    if x_github_event != "pull_request":
        logger.debug("Ignoring event: %s", x_github_event)
        return JSONResponse({"status": "ignored", "event": x_github_event}, status_code=202)

    payload = await request.json()
    action: str = payload.get("action", "")

    # Only review when a PR is opened, updated, or reopened
    if action not in {"opened", "synchronize", "reopened"}:
        logger.debug("Ignoring pull_request action: %s", action)
        return JSONResponse({"status": "ignored", "action": action}, status_code=202)

    # Ensure the payload has an installation (required for App auth)
    if "installation" not in payload:
        logger.error("No installation in payload — is this a GitHub App webhook?")
        raise HTTPException(
            status_code=400,
            detail="Missing installation field. Configure this webhook via a GitHub App.",
        )

    background_tasks.add_task(_handle_pr_event, payload)
    return JSONResponse({"status": "accepted"}, status_code=202)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})
