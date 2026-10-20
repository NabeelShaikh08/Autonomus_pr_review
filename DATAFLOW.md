# Data Flow — Autonomous PR Reviewer

This document traces every piece of data from GitHub webhook to posted review.

---

## 1. Webhook ingestion

```
GitHub
  │
  │  POST /webhook
  │  Headers: X-Hub-Signature-256, X-GitHub-Event, X-GitHub-Delivery
  │  Body: JSON payload (PR metadata, repo, installation ID)
  │
  ▼
app/main.py :: webhook()
  │
  ├── HMAC-SHA256 verify(payload_bytes, X-Hub-Signature-256)
  │     Uses GITHUB_WEBHOOK_SECRET. Rejects with 401 if invalid.
  │
  ├── Parse action → accept only: opened | synchronize | reopened
  │     Others → 202 "ignored"
  │
  ├── Check queue depth via Redis LLEN
  │     depth >= MAX_QUEUE_DEPTH → 503 "busy"
  │
  ├── Deduplication: SADD pr_review_seen {delivery_id}
  │     Already seen → 202 "duplicate" (counter incremented)
  │
  └── LPUSH pr_review_jobs {delivery_id, payload}
        → 202 "accepted"
        (fallback: asyncio.create_task if Redis unavailable)
```

---

## 2. Job queue

```
Redis List: pr_review_jobs
  │
  │  Key: "pr_review_jobs"
  │  Value: JSON { "delivery_id": "...", "payload": { ... } }
  │
  ▼
app/worker.py :: _worker_loop()
  │
  ├── BRPOP pr_review_jobs 2   (blocks up to 2s, then polls)
  ├── asyncio.Semaphore(WORKER_CONCURRENCY)  limits parallel reviews
  └── asyncio.create_task(_handle_pr_event(payload, delivery_id))
```

---

## 3. Pre-review preparation

```
app/main.py :: _handle_pr_event(payload, trace_id)
  │
  ├── Extract: owner, repo_name, pr_number, commit_sha, pr_title, pr_body
  │
  ├── app/auth.py :: get_installation_token(installation_id)
  │     ┌── Check in-process cache (_local_cache)
  │     ├── Check Redis cache  (gh_token:{installation_id}, TTL-aware)
  │     └── GitHub API: POST /app/installations/{id}/access_tokens
  │           JWT signed with GITHUB_PRIVATE_KEY (RS256, 10-min expiry)
  │           Token cached in Redis with TTL (expires - 5 min)
  │
  ├── app/github.py :: get_pr_files(owner, repo, pr_number, token)
  │     GET /repos/{owner}/{repo}/pulls/{number}/files
  │     Paginates (30 files/page), respects MAX_FILES_PER_REVIEW cap
  │     Returns list of {filename, patch, status} dicts
  │
  ├── app/filters.py :: filter_files(raw_files)
  │     Drops files matching ignored_patterns (lock files, binaries,
  │     generated code, docs, assets, etc.)
  │     Returns only reviewable files
  │
  └── app/filters.py :: build_diff_text(reviewable_files)
        Assembles unified diff:
          ### path/to/file.py (modified)
          ```diff
          +added line
          -removed line
          ```
        Truncates at MAX_PATCH_CHARS (80,000 chars)
        Returns (diff_text: str, was_truncated: bool)
```

---

## 4. Multi-agent review pipeline

```
app/agent.py :: run_review(pr_title, pr_body, diff_text)
  │
  ├── _detect_languages(diff_text)
  │     Scans ### file.ext headers → {"Python", "TypeScript", ...}
  │
  ├── Per-agent diff routing
  │     security_diff   = _filter_diff_for_agent(diff, _SECURITY_EXTENSIONS)
  │     performance_diff= _filter_diff_for_agent(diff, _PERFORMANCE_EXTENSIONS)
  │     tests_diff      = _filter_diff_for_agent(diff, _TESTS_EXTENSIONS)
  │     logic_diff      = diff_text  (no filter — reviews all files)
  │
  ├── Prompt enrichment
  │     security_prompt   = base + Python/Go/Java/JS security hints
  │     performance_prompt= base + Python/Go/Java/JS perf hints
  │
  └── asyncio.gather (all four run concurrently)
        │
        ├──────────────────────────────────────────────────────────┐
        ▼                    ▼                   ▼                  ▼
  Security Agent      Logic Agent       Performance Agent    Tests Agent
  (security_diff)    (logic_diff)      (performance_diff)  (tests_diff)
        │                    │                   │                  │
        └────────────────────┴───────────────────┴──────────────────┘
                                    │
                            Each agent runs an agentic loop:
```

---

## 5. Single specialist agent loop

```
_run_specialist_agent(name, system_prompt, user_message, timeout_seconds)
  │
  │  Wrapped in asyncio.wait_for(timeout=specialist_timeout_seconds)
  │  On timeout → SpecialistResult(timed_out=True)
  │
  ├── Build messages:
  │     system: specialist system prompt (+ language hints if enriched)
  │     user:   <pr_metadata> + <diff>  (sanitized, XML-delimited)
  │
  └── Agentic loop (max 10 iterations):
        │
        ├── _call_groq(messages, _SPECIALIST_TOOLS)
        │     Tools offered: add_review_comment | set_specialist_done
        │     Retry on RateLimitError / APITimeoutError (up to 3 attempts)
        │
        ├── Tool: add_review_comment(path, line, risk, body)
        │     Appended to SpecialistResult.comments
        │     Updates issue_count, highest_risk
        │
        ├── Tool: set_specialist_done(summary)
        │     Sets SpecialistResult.summary → exits loop
        │
        └── Finally block (always runs):
              agent_latency_seconds.observe(elapsed)
              agent_comments_total.inc() per comment
              agent_timeouts_total.inc() if timed_out
```

---

## 6. Deduplication + coordinator

```
All four SpecialistResult objects returned from asyncio.gather
  │
  ├── Merge all comments into flat list
  │     all_comments = [c for r in results for c in r.comments]
  │
  ├── _deduplicate_comments(all_comments)
  │     Key: (path, line)
  │     Conflict resolution: keep highest risk (HIGH > MEDIUM > LOW)
  │     → deduped_comments
  │
  └── _run_coordinator_agent(pr_title, pr_body, results, deduped_comments)
        │
        ├── Build coordinator user message:
        │     <pr_metadata>: title + description (sanitized)
        │     <security_findings>:    SpecialistResult.to_coordinator_text()
        │     <logic_findings>:       ...
        │     <performance_findings>: ...
        │     <tests_findings>:       ...
        │     <total_inline_comments>: N after deduplication
        │
        ├── _call_groq(messages, _COORDINATOR_TOOLS)
        │     Tool offered: set_review_summary(summary, verdict)
        │     Verdict enum: APPROVED | NEEDS_WORK | CRITICAL
        │
        └── _format_summary(summary, verdict)
              → "## PR Review ✅ APPROVED\n\n{summary}\n\n---\n*..."
```

---

## 7. Posting the review

```
app/github.py :: post_review(owner, repo, pr_number, commit_sha, token, summary, comments)
  │
  ├── POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews
  │     Body:
  │       commit_id: commit_sha
  │       body:      formatted summary (with verdict + emoji)
  │       event:     "COMMENT"  (never blocks merge)
  │       comments:  [ {path, line, body: formatted_comment} ... ]
  │
  └── Retry on 5xx / connection error (exponential backoff)
```

---

## 8. Audit log

```
app/db.py :: log_review(...)  (always called, even on error)
  │
  ├── asyncpg pool → INSERT INTO reviews
  │     columns: delivery_id, repo_owner, repo_name, pr_number,
  │              verdict, comment_count, latency_ms, error, created_at
  │
  └── Any exception swallowed — DB failure must not affect the review
```

---

## 9. Prometheus metrics (emitted throughout)

| Metric | Emitted at |
|---|---|
| `prreviewer_webhooks_received_total{event,action}` | Webhook ingestion |
| `prreviewer_duplicates_skipped_total` | Dedup hit |
| `prreviewer_queue_depth` | Polled every 15s |
| `prreviewer_agent_latency_seconds{agent}` | End of each specialist |
| `prreviewer_agent_comments_total{agent,risk}` | End of each specialist |
| `prreviewer_agent_timeouts_total{agent}` | Specialist timeout |
| `prreviewer_reviews_total{verdict}` | After coordinator |
| `prreviewer_review_latency_seconds` | After coordinator |
| `prreviewer_github_errors_total{status_code}` | GitHub API errors |
| `prreviewer_groq_errors_total{error_type}` | Groq API errors |

---

## 10. Complete end-to-end sequence

```
GitHub           FastAPI Server        Redis           Worker          Groq API        GitHub API
  │                    │                 │                │                │                │
  │── POST /webhook ──▶│                 │                │                │                │
  │                    │── HMAC verify   │                │                │                │
  │                    │── LLEN check ──▶│                │                │                │
  │                    │── SADD dedup ──▶│                │                │                │
  │                    │── LPUSH job ───▶│                │                │                │
  │◀── 202 accepted ───│                 │                │                │                │
  │                    │                 │◀── BRPOP ──────│                │                │
  │                    │                 │─── job ───────▶│                │                │
  │                    │                 │                │── get token ───────────────────▶│
  │                    │                 │                │◀── token ──────────────────────│
  │                    │                 │                │── get files ───────────────────▶│
  │                    │                 │                │◀── files ──────────────────────│
  │                    │                 │                │                │                │
  │                    │                 │         [asyncio.gather]        │                │
  │                    │                 │                │── security ───▶│                │
  │                    │                 │                │── logic ──────▶│                │
  │                    │                 │                │── performance ▶│                │
  │                    │                 │                │── tests ──────▶│                │
  │                    │                 │                │◀── results ────│                │
  │                    │                 │                │── coordinator ▶│                │
  │                    │                 │                │◀── summary ────│                │
  │                    │                 │                │── post review ─────────────────▶│
  │◀─ inline comments ─────────────────────────────────────────────────────────────────────│
  │                    │                 │                │── log to DB    │                │
```
