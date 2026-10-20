# CLAUDE.md — Context for AI-Assisted Development

This file gives Claude Code the context needed to work effectively on this codebase.

---

## What this project does

Autonomous PR reviewer GitHub App. When a PR is opened or updated, a GitHub webhook fires → FastAPI server verifies it → enqueues a job in Redis → a standalone worker pulls it → 4 specialist AI agents run in parallel on the diff → a coordinator agent synthesises findings → review is posted back to GitHub as inline comments + a top-level summary.

---

## Architecture at a glance

```
GitHub webhook
    │
    ▼
POST /webhook  (app/main.py)
    │  HMAC-SHA256 verified, delivery ID deduped via Redis SET
    │
    ▼
Redis queue  (LPUSH)  ←──  app/queue.py
    │
    ▼
Worker process  (app/worker.py)
    │  asyncio.Semaphore(concurrency) limits parallel reviews
    │  SIGTERM → drain in-flight → exit
    │
    ▼
_handle_pr_event()  (app/main.py)
    │  fetch token → fetch files → filter → build diff
    │
    ▼
run_review()  (app/agent.py)   ← public entry point
    │
    ├── asyncio.gather (all four run concurrently)
    │       ├── _run_specialist_agent("security",  ...)
    │       ├── _run_specialist_agent("logic",     ...)
    │       ├── _run_specialist_agent("performance", ...)
    │       └── _run_specialist_agent("tests",     ...)
    │
    ├── _deduplicate_comments()   same file+line → keep highest risk
    │
    └── _run_coordinator_agent()  → formatted summary + verdict
            │
            ▼
    post_review()  (app/github.py)  → inline comments + PR review body
            │
            ▼
    log_review()  (app/db.py)  → PostgreSQL audit row (best-effort)
```

---

## Key files

| File | Purpose |
|---|---|
| `app/main.py` | FastAPI app, webhook endpoint, `_handle_pr_event`, lifespan |
| `app/agent.py` | Complete multi-agent pipeline — all specialist + coordinator logic |
| `app/auth.py` | GitHub App JWT → installation token, 3-layer Redis cache |
| `app/github.py` | GitHub REST API: fetch files, post review, retry logic |
| `app/filters.py` | File filtering (ignored patterns) + diff building |
| `app/queue.py` | Redis LPUSH/BRPOP job queue + SADD-based deduplication |
| `app/worker.py` | Standalone async process, concurrency semaphore, graceful shutdown |
| `app/db.py` | asyncpg pool, auto-creates `reviews` table, best-effort inserts |
| `app/config.py` | All env vars as pydantic-settings `Settings` dataclass |
| `app/redis_client.py` | Shared Redis pool — `socket_timeout=None` required for BRPOP |
| `app/metrics.py` | All Prometheus counters/histograms/gauges |
| `app/logging_config.py` | Structured JSON logging + `ContextVar` trace ID per request |

---

## Multi-agent system — important details

### Specialist agents (`_run_specialist_agent`)

- Each runs its own agentic loop (max 10 iterations, Groq tool-use)
- Tools: `add_review_comment` (collects findings) + `set_specialist_done` (signals completion)
- Wrapped in `asyncio.wait_for(timeout=specialist_timeout_seconds)` — never blocks forever
- Returns `SpecialistResult` dataclass (comments, issue_count, highest_risk, summary, timed_out)
- On timeout/error: returns a result with `timed_out=True`, never raises

### File-type routing

Agents only receive diff content for relevant file extensions:
- Security: source code only (`.py`, `.js`, `.ts`, `.go`, `.java`, `.sh`, `.sql`, etc.)
- Performance: same as security minus shell/config
- Logic: full diff (no filter)
- Tests: source files only (same as security, minus config/shell)

If no files match an agent's extension filter, it gets the full diff (fallback).

### Language-aware prompt enrichment

`_detect_languages()` scans diff headers for file extensions → `_enrich_prompt()` appends language-specific vulnerability patterns (Python `eval()`, Go `os/exec`, etc.) to security and performance system prompts.

### Deduplication

`_deduplicate_comments()` — same `(path, line)` key → keep highest risk. Applied before coordinator sees the comment list and before posting to GitHub.

### Groq singleton

`_groq_client` is a module-level singleton. In tests: `agent_module._groq_client = None` in `setup_method` to prevent cross-test leakage. Patch `_get_groq_client` not `Groq`.

### Coordinator input

`SpecialistResult.to_coordinator_text()` renders agent name, issue count, highest risk, summary, and per-comment detail. Wrapped in `<{name}_findings>` XML tags before being sent to the coordinator so it reads structured data, not free text.

---

## Testing

```bash
pytest tests/ -v
```

52 tests across 6 files:

| Test file | What it covers |
|---|---|
| `test_agent.py` | Multi-agent pipeline, deduplication, file-type routing, language detection |
| `test_auth.py` | Token caching (local + Redis), JWT, concurrent token refresh |
| `test_filters.py` | File filtering, ignored patterns, `build_diff_text` tuple return |
| `test_github.py` | GitHub API retries, pagination, rate limit handling |
| `test_webhook.py` | Webhook routing, HMAC signature verification |
| `test_worker.py` | Worker concurrency, graceful shutdown, trace ID per job |

### Test-specific patterns

- `_mock_settings()` helper always returns explicit integer timeouts — never let `MagicMock` auto-return bleed into `asyncio.wait_for(timeout=...)`.
- `build_diff_text` returns `(str, bool)` — tests must unpack the tuple.
- In `test_auth.py`, the in-process cache is named `_local_cache` (not `_token_cache`).

---

## Configuration

All config lives in `app/config.py` as `Settings` (pydantic-settings). Use `get_settings()` (LRU-cached). Never import `Settings` directly in production code.

Key non-obvious settings:
- `specialist_timeout_seconds = 180` — per-agent hard cap; coordinator gets `coordinator_timeout_seconds = 120`
- `max_queue_depth = 100` — webhook returns 503 when Redis queue is ≥ this depth
- `socket_timeout=None` in `redis_client.py` — critical: any finite timeout breaks BRPOP

---

## Redis usage

- Job queue: `LPUSH pr_review_jobs` / `BRPOP pr_review_jobs 2` (2s poll)
- Dedup: `SADD pr_review_seen {delivery_id}` with TTL `dedup_ttl_seconds`
- Token cache: `gh_token:{installation_id}` with TTL (refreshed 5 min before expiry)
- Queue depth: `LLEN pr_review_jobs`

---

## Deployment

Railway.app — two services from the same repo:
- **Web**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
- **Worker**: `python -m app.worker`

Both need the same env vars. `DATABASE_URL` is optional (audit log disabled if absent).

---

## What to be careful about

1. **Never add Co-Authored-By lines to commits.** Commits are authored by Nabeel only.
2. **`socket_timeout=None` on Redis pool** — required for BRPOP. Any finite value will cause periodic `TimeoutError` crashes in the worker.
3. **`enqueue_review` imported at module level in `main.py`** — not inside the webhook handler. This is required so `patch("app.main.enqueue_review")` works in tests.
4. **`build_diff_text` returns `(str, bool)`** — the bool is `was_truncated`. Always unpack.
5. **`_groq_client` singleton** — reset in test `setup_method`, not after each test.
6. **Prompt injection defence** — PR title/body is sanitized with `_sanitize()` and wrapped in XML delimiters before going to any agent.
7. **PostgreSQL is best-effort** — `log_review()` never raises; a DB failure must not prevent the review from being posted.
