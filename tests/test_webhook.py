"""Tests for webhook endpoint — signature verification and routing."""

import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

SECRET = "test-webhook-secret"


def _make_settings(secret: str = SECRET):
    s = MagicMock()
    s.github_webhook_secret = secret
    s.log_level = "INFO"
    s.is_configured.return_value = True
    s.github_app_id = "123"
    return s


def _sign(payload: bytes, secret: str = SECRET) -> str:
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _pr_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": 42,
            "title": "Test PR",
            "body": "Description",
            "head": {"sha": "abc123def456"},
        },
        "repository": {
            "name": "my-repo",
            "owner": {"login": "octocat"},
        },
        "installation": {"id": 99},
    }


@pytest.fixture
def client():
    """TestClient with get_settings mocked throughout."""
    with patch("app.main.get_settings", return_value=_make_settings()):
        from app.main import app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


class TestSignatureVerification:
    def test_rejects_missing_signature(self, client):
        resp = client.post(
            "/webhook",
            content=b"{}",
            headers={"X-GitHub-Event": "ping"},
        )
        assert resp.status_code == 401

    def test_rejects_wrong_signature(self, client):
        payload = b'{"action": "opened"}'
        resp = client.post(
            "/webhook",
            content=payload,
            headers={
                "X-GitHub-Event": "ping",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
        assert resp.status_code == 401

    def test_accepts_valid_signature(self, client):
        payload = json.dumps(_pr_payload()).encode()
        with patch("app.main.enqueue_review", new_callable=AsyncMock, return_value=True):
            resp = client.post(
                "/webhook",
                content=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": _sign(payload),
                    "X-GitHub-Delivery": "test-delivery-1",
                },
            )
        assert resp.status_code == 202


class TestEventRouting:
    def _post(self, client, event: str, payload: dict, sign_secret: str = SECRET):
        body = json.dumps(payload).encode()
        return client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": _sign(body, sign_secret),
                "X-GitHub-Delivery": "test-delivery-routing",
            },
        )

    def test_ignores_non_pr_events(self, client):
        resp = self._post(client, "push", {"ref": "refs/heads/main"})
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"

    def test_ignores_pr_closed_action(self, client):
        resp = self._post(client, "pull_request", _pr_payload(action="closed"))
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"

    def test_accepts_pr_opened(self, client):
        with patch("app.main.enqueue_review", new_callable=AsyncMock, return_value=True):
            resp = self._post(client, "pull_request", _pr_payload(action="opened"))
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_accepts_pr_synchronize(self, client):
        with patch("app.main.enqueue_review", new_callable=AsyncMock, return_value=True):
            resp = self._post(client, "pull_request", _pr_payload(action="synchronize"))
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_accepts_pr_reopened(self, client):
        with patch("app.main.enqueue_review", new_callable=AsyncMock, return_value=True):
            resp = self._post(client, "pull_request", _pr_payload(action="reopened"))
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_rejects_missing_installation(self, client):
        payload = {
            "action": "opened",
            "pull_request": {"number": 1, "title": "x", "body": "", "head": {"sha": "abc"}},
            "repository": {"name": "repo", "owner": {"login": "user"}},
            # no "installation" key
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": _sign(body),
            },
        )
        assert resp.status_code == 400


class TestHealthCheck:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] in ("ok", "degraded")
        assert "checks" in resp.json()
