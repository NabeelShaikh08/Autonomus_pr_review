# Autonomous PR Reviewer

A production-grade GitHub App that automatically reviews every pull request using a multi-agent AI pipeline. When a PR is opened or updated, four specialist AI agents analyse the diff concurrently and post inline comments with a coordinated summary — no manual steps required.

---

## How it works

```
PR opened/updated
       │
       ▼
GitHub webhook → FastAPI server → Redis queue
                                       │
                                       ▼
                               Worker process pulls job
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼             ▼            ▼
                    Security       Logic        Performance    Tests
                     Agent         Agent          Agent        Agent
                    (parallel)   (parallel)     (parallel)  (parallel)
                          │            │             │            │
                          └────────────┴─────────────┘────────────┘
                                       │
                                  Deduplication
                                       │
                                       ▼
                               Coordinator Agent
                                       │
                                       ▼
                            Review posted to GitHub PR
                            + Audit row written to PostgreSQL
```

See [DATAFLOW.md](DATAFLOW.md) for the full data flow diagram.

---

## What each agent reviews

| Agent | Focuses on |
|---|---|
| **Security** | Injection, auth flaws, exposed secrets, insecure defaults |
| **Logic** | Bugs, None dereferences, race conditions, broken control flow |
| **Performance** | N+1 queries, blocking calls, unbounded loops, memory issues |
| **Tests** | Critical new code with no test coverage |
| **Coordinator** | Synthesises all findings, deduplicates, writes final summary + verdict |

All four specialists run **concurrently** — total review time is roughly equal to one agent, not four.

Each specialist only receives files relevant to its domain (file-type routing), and system prompts are enriched with language-specific patterns for Python, JavaScript, TypeScript, Go, Java, and more.

---

## Verdicts

Every review ends with one of three verdicts:

- **APPROVED** — no meaningful issues found, safe to merge
- **NEEDS_WORK** — real issues that should be addressed before merging
- **CRITICAL** — bugs, data loss risk, or security vulnerabilities that must be fixed

---

## Production infrastructure

| Component | Technology | Purpose |
|---|---|---|
| Web server | FastAPI + uvicorn | Receives webhooks, enqueues jobs |
| Job queue | Redis (LPUSH/BRPOP) | Decouples ingestion from processing |
| Worker | Standalone async process | Pulls jobs, runs review pipeline |
| Deduplication | Redis SET | Prevents duplicate reviews on retried webhooks |
| Audit log | PostgreSQL (asyncpg) | Permanent record of every review |
| Metrics | Prometheus (`/metrics`) | Reviews, errors, latency, queue depth, per-agent stats |
| Logging | Structured JSON + trace IDs | Every log line tied to a GitHub delivery ID |

---

## Self-hosting guide

### 1. Create a GitHub App

1. Go to **github.com/settings/apps/new**
2. Fill in:
   - **App name**: anything unique (e.g. `my-pr-reviewer`)
   - **Homepage URL**: your repo URL
   - **Webhook URL**: your server URL + `/webhook` (update after deployment)
   - **Webhook secret**: click **Generate a random token** — save it
3. Under **Repository permissions**:
   - `Pull requests` → **Read & write**
   - `Contents` → **Read-only**
4. Under **Subscribe to events**: check **Pull request**
5. Click **Create GitHub App**
6. Note your **App ID** from the settings page
7. Scroll down → **Generate a private key** → download the `.pem` file

### 2. Get a Groq API key

Sign up at **console.groq.com** and create a free API key.

### 3. Environment variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `GITHUB_APP_ID` | Yes | App ID from step 1 |
| `GITHUB_PRIVATE_KEY` | Yes | Full contents of the `.pem` file (newlines as `\n`) |
| `GITHUB_WEBHOOK_SECRET` | Yes | The secret you set in step 1 |
| `GROQ_API_KEY` | Yes | Your Groq API key |
| `REDIS_URL` | Yes (prod) | Redis connection URL |
| `DATABASE_URL` | No | PostgreSQL URL for audit log |

Optional tuning:

| Variable | Default | Description |
|---|---|---|
| `GROQ_MODEL` | `qwen/qwen3.8-27b` | Groq model to use |
| `MAX_FILES_PER_REVIEW` | `50` | Max files reviewed per PR |
| `MAX_PATCH_CHARS` | `80000` | Truncate diff beyond this size |
| `MAX_QUEUE_DEPTH` | `100` | Reject webhooks when queue is this deep |
| `WORKER_CONCURRENCY` | `3` | Concurrent reviews per worker |
| `DEDUP_TTL_SECONDS` | `3600` | How long to remember delivery IDs |
| `SPECIALIST_TIMEOUT_SECONDS` | `180` | Per-agent timeout (3 min) |
| `COORDINATOR_TIMEOUT_SECONDS` | `120` | Coordinator timeout (2 min) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### 4. Deploy to Railway (recommended)

**Web service:**
1. Push this repo to GitHub
2. Go to **railway.app** → New Project → Deploy from GitHub repo
3. Select your repo → Deploy Now
4. Go to **Settings → Networking → Generate Domain** (port `8000`)
5. Add all required environment variables
6. Copy your Railway domain and update the **Webhook URL** in your GitHub App settings to `https://your-domain.up.railway.app/webhook`

**Add Redis:**
1. In your Railway project → **+ New** → **Database** → **Add Redis**
2. Copy `REDIS_URL` from the Redis service Variables tab into your web service

**Add PostgreSQL (optional, for audit log):**
1. In your Railway project → **+ New** → **Database** → **Add PostgreSQL**
2. Copy `DATABASE_URL` from the PostgreSQL service Variables tab into your web service

**Add worker service:**
1. **+ New** → **GitHub Repo** → same repo
2. Set **Start Command** to: `python -m app.worker`
3. Add the same environment variables as the web service

### 5. Install the app on a repository

1. GitHub App settings → **Install App**
2. Choose the repositories you want reviewed
3. Open a PR — the bot reviews it automatically

---

## Running locally (for development)

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
uvicorn app.main:app --reload --port 8000

# Expose it to the internet (in another terminal)
ngrok http 8000
# Paste the ngrok URL into your GitHub App webhook settings
```

Generate a webhook secret:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Verify your config loaded correctly:
```bash
python -c "from app.config import get_settings; s = get_settings(); print(s.is_configured())"
```

Run the worker locally (needs Redis running):
```bash
python -m app.worker
```

---

## Running with Docker

```bash
docker build -t pr-reviewer .
docker run --env-file .env -p 8000:8000 pr-reviewer
```

---

## Running tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

52 tests covering: webhook routing, signature verification, token caching, file filtering, GitHub API retries, multi-agent review pipeline, deduplication, file-type routing, language detection, worker concurrency, and graceful shutdown.

---

## Observability

**Health check:**
```
GET /health
```
Returns status of GitHub API, Groq API, Redis, PostgreSQL, and current queue depth.

**Metrics:**
```
GET /metrics
```
Prometheus-compatible metrics including:
- `prreviewer_reviews_total{verdict}` — reviews by outcome
- `prreviewer_review_latency_seconds` — end-to-end latency histogram
- `prreviewer_agent_comments_total{agent, risk}` — comments per specialist
- `prreviewer_agent_latency_seconds{agent}` — per-agent timing
- `prreviewer_agent_timeouts_total{agent}` — agent timeouts
- `prreviewer_queue_depth` — current Redis queue depth
- `prreviewer_github_errors_total{status_code}` — GitHub API errors
- `prreviewer_groq_errors_total{error_type}` — Groq API errors

---

## Project structure

```
├── app/
│   ├── main.py           # FastAPI app, webhook endpoint, orchestration
│   ├── agent.py          # Multi-agent pipeline (4 specialists + coordinator)
│   ├── auth.py           # GitHub App JWT + Redis-cached installation tokens
│   ├── github.py         # GitHub API: fetch files, post review
│   ├── filters.py        # File filtering and diff building
│   ├── config.py         # All environment variable configuration
│   ├── queue.py          # Redis job queue + webhook deduplication
│   ├── worker.py         # Standalone async worker process
│   ├── db.py             # PostgreSQL audit log
│   ├── redis_client.py   # Shared Redis connection pool
│   ├── metrics.py        # Prometheus metrics definitions
│   └── logging_config.py # Structured JSON logging + trace IDs
├── tests/
│   ├── test_agent.py     # Multi-agent pipeline, deduplication, routing
│   ├── test_auth.py      # Token caching, JWT, concurrency
│   ├── test_filters.py   # File filtering, diff truncation
│   ├── test_github.py    # GitHub API retries, pagination
│   ├── test_webhook.py   # Webhook routing, signature verification
│   └── test_worker.py    # Worker concurrency, graceful shutdown
├── Dockerfile
├── Procfile              # Railway: web + worker process types
├── CLAUDE.md             # Context file for AI-assisted development
├── DATAFLOW.md           # Data flow diagram across agents
├── .env.example
└── requirements.txt
```

---

## Security

- **Webhook signature verification** — every incoming webhook is HMAC-SHA256 verified before processing
- **Prompt injection defence** — PR titles and descriptions are sanitized and wrapped in XML delimiters before being sent to the AI
- **Token caching** — GitHub installation tokens are cached in Redis and refreshed 5 minutes before expiry
- **Queue overflow protection** — webhooks are rejected with 503 when queue depth exceeds `MAX_QUEUE_DEPTH`
- **Non-root Docker** — container runs as `appuser` (UID 1000), not root
