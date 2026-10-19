"""
Prometheus metrics for the PR reviewer.
Exposed at GET /metrics.
"""

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

__all__ = [
    "reviews_total",
    "github_errors_total",
    "groq_errors_total",
    "webhooks_received_total",
    "duplicates_skipped_total",
    "review_latency_seconds",
    "queue_depth_gauge",
    "generate_latest",
    "CONTENT_TYPE_LATEST",
]

reviews_total = Counter(
    "prreviewer_reviews_total",
    "Total reviews completed",
    ["verdict"],
)

github_errors_total = Counter(
    "prreviewer_github_errors_total",
    "GitHub API errors by HTTP status code",
    ["status_code"],
)

groq_errors_total = Counter(
    "prreviewer_groq_errors_total",
    "Groq API errors by error type",
    ["error_type"],
)

webhooks_received_total = Counter(
    "prreviewer_webhooks_received_total",
    "Webhooks received by event and action",
    ["event", "action"],
)

duplicates_skipped_total = Counter(
    "prreviewer_duplicates_skipped_total",
    "Duplicate webhook deliveries skipped",
)

review_latency_seconds = Histogram(
    "prreviewer_review_latency_seconds",
    "End-to-end review latency in seconds",
    buckets=[5, 10, 20, 30, 60, 120, 300],
)

queue_depth_gauge = Gauge(
    "prreviewer_queue_depth",
    "Current number of jobs waiting in the Redis review queue",
)
