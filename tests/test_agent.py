"""Tests for the Groq agentic review loop."""

import json
import pytest
from unittest.mock import MagicMock, patch


def _make_tool_call(name: str, args: dict, call_id: str = "call_1"):
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    return tc


def _make_response(tool_calls=None, finish_reason="tool_calls", content=""):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestRunReview:
    def setup_method(self):
        # Reset singleton so each test gets a fresh mock client
        import app.agent as agent_module
        agent_module._groq_client = None

    @pytest.mark.asyncio
    async def test_returns_summary_on_approved_pr(self):
        from app.agent import run_review

        summary_call = _make_tool_call("set_review_summary", {
            "summary": "Looks good.",
            "verdict": "APPROVED",
        })
        response = _make_response(tool_calls=[summary_call])

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = response

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings") as mock_settings:
            mock_settings.return_value.groq_model = "test-model"

            summary, comments = await run_review("Fix bug", "", "+ fixed = True")

        assert "APPROVED" in summary
        assert comments == []

    @pytest.mark.asyncio
    async def test_collects_inline_comments(self):
        from app.agent import run_review

        comment_call = _make_tool_call("add_review_comment", {
            "path": "src/app.py",
            "line": 10,
            "risk": "HIGH",
            "body": "This will crash.",
        }, call_id="call_1")
        summary_call = _make_tool_call("set_review_summary", {
            "summary": "Has issues.",
            "verdict": "CRITICAL",
        }, call_id="call_2")

        resp1 = _make_response(tool_calls=[comment_call])
        resp2 = _make_response(tool_calls=[summary_call])

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [resp1, resp2]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings") as mock_settings:
            mock_settings.return_value.groq_model = "test-model"

            summary, comments = await run_review("Add feature", "", "+ x = None\n+ x.method()")

        assert "CRITICAL" in summary
        assert len(comments) == 1
        assert comments[0]["path"] == "src/app.py"
        assert comments[0]["line"] == 10
        assert "HIGH" in comments[0]["body"]

    @pytest.mark.asyncio
    async def test_handles_empty_choices_gracefully(self):
        from app.agent import run_review

        empty_resp = MagicMock()
        empty_resp.choices = []

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = empty_resp

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings") as mock_settings:
            mock_settings.return_value.groq_model = "test-model"

            summary, comments = await run_review("Test", "", "+ x = 1")

        assert summary == "_Review completed._"
        assert comments == []

    @pytest.mark.asyncio
    async def test_handles_invalid_tool_args(self):
        from app.agent import run_review

        bad_tc = MagicMock()
        bad_tc.id = "call_bad"
        bad_tc.function.name = "add_review_comment"
        bad_tc.function.arguments = "not valid json {"

        summary_call = _make_tool_call("set_review_summary", {
            "summary": "Done.",
            "verdict": "APPROVED",
        }, call_id="call_summary")

        resp1 = _make_response(tool_calls=[bad_tc])
        resp2 = _make_response(tool_calls=[summary_call])

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [resp1, resp2]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings") as mock_settings:
            mock_settings.return_value.groq_model = "test-model"

            summary, comments = await run_review("Test", "", "+ x = 1")

        assert "APPROVED" in summary
        # The bad tool call produces an empty-path comment; summary still succeeds
        assert all(c["path"] == "" for c in comments)

    @pytest.mark.asyncio
    async def test_retries_on_groq_rate_limit(self):
        from app.agent import run_review
        from groq import RateLimitError

        summary_call = _make_tool_call("set_review_summary", {
            "summary": "Good.",
            "verdict": "APPROVED",
        })
        good_resp = _make_response(tool_calls=[summary_call])

        mock_rate_limit = RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body={},
        )

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [mock_rate_limit, good_resp]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings") as mock_settings, \
             patch("asyncio.sleep", return_value=None):
            mock_settings.return_value.groq_model = "test-model"

            summary, comments = await run_review("Test", "", "+ x = 1")

        assert "APPROVED" in summary
