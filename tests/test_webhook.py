"""Tests for webhook endpoint — signature verification and routing."""

import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.main import app

client = TestClient(app)

SECRET = "test-webhook-secret"


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


class TestSignatureVerification:
    def test_rejects_missing_signature(self):
        resp = client.post(
            "/webhook",
            content=b"{}",
            headers={"X-GitHub-Event": "pull_request"},
        )
        assert resp.status_code == 401

    def test_rejects_wrong_signature(self):
        payload = b'{"action": "opened"}'
        resp = client.post(
            "/webhook",
            content=payload,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
        assert resp.status_code == 401

    def test_accepts_valid_signature(self):
        payload = json.dumps(_pr_payload()).encode()
        with (
            patch("app.main.get_settings") as mock_settings,
            patch("app.main._handle_pr_event", new_callable=AsyncMock),
        ):
            mock_settings.return_value.github_webhook_secret = SECRET
            mock_settings.return_value.log_level = "INFO"
            resp = client.post(
                "/webhook",
                content=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": _sign(payload),
                },
            )
        assert resp.status_code == 202


class TestEventRouting:
    def _post(self, event: str, payload: dict):
        body = json.dumps(payload).encode()
        with patch("app.main.get_settings") as mock_settings:
            mock_settings.return_value.github_webhook_secret = SECRET
            mock_settings.return_value.log_level = "INFO"
            return client.post(
                "/webhook",
                content=body,
                headers={
                    "X-GitHub-Event": event,
                    "X-Hub-Signature-256": _sign(body),
                },
            )

    def test_ignores_non_pr_events(self):
        resp = self._post("push", {"ref": "refs/heads/main"})
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"

    def test_ignores_pr_closed_action(self):
        resp = self._post("pull_request", _pr_payload(action="closed"))
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"

    def test_accepts_pr_opened(self):
        with patch("app.main._handle_pr_event", new_callable=AsyncMock):
            resp = self._post("pull_request", _pr_payload(action="opened"))
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_accepts_pr_synchronize(self):
        with patch("app.main._handle_pr_event", new_callable=AsyncMock):
            resp = self._post("pull_request", _pr_payload(action="synchronize"))
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"


class TestHealthCheck:
    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
