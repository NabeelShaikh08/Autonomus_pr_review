"""Tests for the multi-agent PR review pipeline."""

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


def _mock_settings():
    s = MagicMock()
    s.groq_model = "test-model"
    s.specialist_timeout_seconds = 30
    s.coordinator_timeout_seconds = 20
    return s


def _done_response(name="security"):
    """Specialist signals done with no findings."""
    return _make_response(tool_calls=[
        _make_tool_call("set_specialist_done", {"summary": "No issues found."}, call_id=f"done_{name}")
    ])


def _coordinator_response(summary="All good.", verdict="APPROVED"):
    return _make_response(tool_calls=[
        _make_tool_call("set_review_summary", {"summary": summary, "verdict": verdict}, call_id="coord_1")
    ])


class TestRunReview:
    def setup_method(self):
        import app.agent as agent_module
        agent_module._groq_client = None

    @pytest.mark.asyncio
    async def test_approved_pr_no_comments(self):
        """All specialists find nothing → coordinator approves."""
        from app.agent import run_review

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _done_response("security"),
            _done_response("logic"),
            _done_response("performance"),
            _done_response("tests"),
            _coordinator_response("Looks clean.", "APPROVED"),
        ]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings", return_value=_mock_settings()):
            summary, comments = await run_review("Fix typo", "", "+ x = 1")

        assert "APPROVED" in summary
        assert comments == []

    @pytest.mark.asyncio
    async def test_collects_comments_from_all_specialists(self):
        """Each specialist finds one issue → all appear in final comments."""
        from app.agent import run_review

        sec_comment = _make_tool_call("add_review_comment", {
            "path": "auth.py", "line": 5, "risk": "HIGH", "body": "SQL injection risk.",
        }, call_id="s1")
        logic_comment = _make_tool_call("add_review_comment", {
            "path": "utils.py", "line": 12, "risk": "MEDIUM", "body": "None dereference.",
        }, call_id="l1")
        perf_comment = _make_tool_call("add_review_comment", {
            "path": "db.py", "line": 20, "risk": "LOW", "body": "N+1 query.",
        }, call_id="p1")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _make_response(tool_calls=[sec_comment]),
            _make_response(tool_calls=[_make_tool_call("set_specialist_done", {"summary": "SQL injection found."}, call_id="sd1")]),
            _make_response(tool_calls=[logic_comment]),
            _make_response(tool_calls=[_make_tool_call("set_specialist_done", {"summary": "None dereference found."}, call_id="ld1")]),
            _make_response(tool_calls=[perf_comment]),
            _make_response(tool_calls=[_make_tool_call("set_specialist_done", {"summary": "N+1 found."}, call_id="pd1")]),
            _done_response("tests"),
            _coordinator_response("Multiple issues found.", "CRITICAL"),
        ]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings", return_value=_mock_settings()):
            summary, comments = await run_review("Big PR", "", "+ code")

        assert "CRITICAL" in summary
        assert len(comments) == 3
        paths = {c["path"] for c in comments}
        assert paths == {"auth.py", "utils.py", "db.py"}

    @pytest.mark.asyncio
    async def test_deduplicates_same_file_and_line(self):
        """Two specialists flag the same file+line → only the higher-risk one kept."""
        from app.agent import run_review

        sec_comment = _make_tool_call("add_review_comment", {
            "path": "auth.py", "line": 5, "risk": "HIGH", "body": "Injection.",
        }, call_id="s1")
        logic_comment = _make_tool_call("add_review_comment", {
            "path": "auth.py", "line": 5, "risk": "MEDIUM", "body": "Also a bug.",
        }, call_id="l1")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _make_response(tool_calls=[sec_comment]),
            _make_response(tool_calls=[_make_tool_call("set_specialist_done", {"summary": "Injection."}, call_id="sd1")]),
            _make_response(tool_calls=[logic_comment]),
            _make_response(tool_calls=[_make_tool_call("set_specialist_done", {"summary": "Bug."}, call_id="ld1")]),
            _done_response("performance"),
            _done_response("tests"),
            _coordinator_response("One issue.", "CRITICAL"),
        ]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings", return_value=_mock_settings()):
            summary, comments = await run_review("PR", "", "+ code")

        assert len(comments) == 1
        assert comments[0]["risk"] == "HIGH"

    @pytest.mark.asyncio
    async def test_handles_empty_choices_gracefully(self):
        """Empty Groq response doesn't crash — returns default summary."""
        from app.agent import run_review

        empty_resp = MagicMock()
        empty_resp.choices = []

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = empty_resp

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings", return_value=_mock_settings()):
            summary, comments = await run_review("Test", "", "+ x = 1")

        assert isinstance(summary, str)
        assert isinstance(comments, list)

    @pytest.mark.asyncio
    async def test_handles_invalid_tool_args(self):
        """Malformed JSON in tool args doesn't crash the loop."""
        from app.agent import run_review

        bad_tc = MagicMock()
        bad_tc.id = "call_bad"
        bad_tc.function.name = "add_review_comment"
        bad_tc.function.arguments = "not valid json {"

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _make_response(tool_calls=[bad_tc]),
            _make_response(tool_calls=[_make_tool_call("set_specialist_done", {"summary": "Done."}, call_id="sd1")]),
            _done_response("logic"),
            _done_response("performance"),
            _done_response("tests"),
            _coordinator_response("Looks fine.", "APPROVED"),
        ]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings", return_value=_mock_settings()):
            summary, comments = await run_review("Test", "", "+ x = 1")

        assert "APPROVED" in summary

    @pytest.mark.asyncio
    async def test_retries_on_groq_rate_limit(self):
        """Rate limit error is retried; review still succeeds."""
        from app.agent import run_review
        from groq import RateLimitError

        mock_rate_limit = RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body={},
        )

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            mock_rate_limit,
            _done_response("security"),
            _done_response("logic"),
            _done_response("performance"),
            _done_response("tests"),
            _coordinator_response("Good.", "APPROVED"),
        ]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings", return_value=_mock_settings()), \
             patch("asyncio.sleep", return_value=None):
            summary, comments = await run_review("Test", "", "+ x = 1")

        assert "APPROVED" in summary

    @pytest.mark.asyncio
    async def test_tests_agent_flags_missing_coverage(self):
        """Tests specialist flags untested critical code."""
        from app.agent import run_review

        test_comment = _make_tool_call("add_review_comment", {
            "path": "payments.py", "line": 42, "risk": "HIGH",
            "body": "New payment processing function has no test coverage.",
        }, call_id="t1")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _done_response("security"),
            _done_response("logic"),
            _done_response("performance"),
            _make_response(tool_calls=[test_comment]),
            _make_response(tool_calls=[_make_tool_call("set_specialist_done", {"summary": "Missing tests found."}, call_id="td1")]),
            _coordinator_response("Missing test coverage.", "NEEDS_WORK"),
        ]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings", return_value=_mock_settings()):
            summary, comments = await run_review("Add payment flow", "", "+ def process_payment(): pass")

        assert "NEEDS_WORK" in summary
        assert any(c["path"] == "payments.py" for c in comments)

    @pytest.mark.asyncio
    async def test_specialist_result_structured(self):
        """SpecialistResult captures issue_count and highest_risk correctly."""
        from app.agent import _run_specialist_agent, _SECURITY_SYSTEM_PROMPT

        high_comment = _make_tool_call("add_review_comment", {
            "path": "a.py", "line": 1, "risk": "HIGH", "body": "Critical issue.",
        }, call_id="c1")
        low_comment = _make_tool_call("add_review_comment", {
            "path": "b.py", "line": 2, "risk": "LOW", "body": "Minor issue.",
        }, call_id="c2")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _make_response(tool_calls=[high_comment, low_comment]),
            _make_response(tool_calls=[_make_tool_call("set_specialist_done", {"summary": "Two issues."}, call_id="d1")]),
        ]

        with patch("app.agent._get_groq_client", return_value=mock_client), \
             patch("app.agent.get_settings", return_value=_mock_settings()):
            result = await _run_specialist_agent("security", _SECURITY_SYSTEM_PROMPT, "diff text", timeout_seconds=30)

        assert result.issue_count == 2
        assert result.highest_risk == "HIGH"
        assert result.summary == "Two issues."
        assert result.timed_out is False


class TestDeduplication:
    def test_keeps_unique_comments(self):
        from app.agent import _deduplicate_comments
        comments = [
            {"path": "a.py", "line": 1, "risk": "HIGH", "body": "x"},
            {"path": "b.py", "line": 2, "risk": "LOW", "body": "y"},
        ]
        result = _deduplicate_comments(comments)
        assert len(result) == 2

    def test_deduplicates_same_path_and_line(self):
        from app.agent import _deduplicate_comments
        comments = [
            {"path": "a.py", "line": 5, "risk": "LOW", "body": "low"},
            {"path": "a.py", "line": 5, "risk": "HIGH", "body": "high"},
            {"path": "a.py", "line": 5, "risk": "MEDIUM", "body": "medium"},
        ]
        result = _deduplicate_comments(comments)
        assert len(result) == 1
        assert result[0]["risk"] == "HIGH"

    def test_different_lines_same_file_not_deduplicated(self):
        from app.agent import _deduplicate_comments
        comments = [
            {"path": "a.py", "line": 1, "risk": "HIGH", "body": "x"},
            {"path": "a.py", "line": 2, "risk": "HIGH", "body": "y"},
        ]
        result = _deduplicate_comments(comments)
        assert len(result) == 2


class TestFileTypeRouting:
    def test_security_filters_out_css(self):
        from app.agent import _filter_diff_for_agent, _SECURITY_EXTENSIONS
        diff = (
            "### styles.css (modified)\n```diff\n+.button { color: red; }\n```\n\n"
            "### auth.py (modified)\n```diff\n+def login(): pass\n```"
        )
        result = _filter_diff_for_agent(diff, _SECURITY_EXTENSIONS)
        assert "auth.py" in result
        assert "styles.css" not in result

    def test_returns_full_diff_when_no_match(self):
        from app.agent import _filter_diff_for_agent
        diff = "### README.md (modified)\n```diff\n+# New section\n```"
        result = _filter_diff_for_agent(diff, {".py"})
        # No .py files → falls back to full diff
        assert "README.md" in result

    def test_language_detection(self):
        from app.agent import _detect_languages
        diff = (
            "### app/main.py (modified)\n```diff\n+x = 1\n```\n\n"
            "### frontend/app.ts (added)\n```diff\n+const x = 1\n```"
        )
        langs = _detect_languages(diff)
        assert "Python" in langs
        assert "TypeScript" in langs

    def test_enrich_prompt_adds_hints(self):
        from app.agent import _enrich_prompt, _LANGUAGE_SECURITY_HINTS
        base = "Base prompt."
        result = _enrich_prompt(base, _LANGUAGE_SECURITY_HINTS, {"Python"})
        assert "eval()" in result
        assert base in result

    def test_enrich_prompt_no_hints_for_unknown_language(self):
        from app.agent import _enrich_prompt, _LANGUAGE_SECURITY_HINTS
        base = "Base prompt."
        result = _enrich_prompt(base, _LANGUAGE_SECURITY_HINTS, {"COBOL"})
        assert result == base


class TestSpecialistResult:
    def test_to_coordinator_text_no_issues(self):
        from app.agent import SpecialistResult
        r = SpecialistResult(name="security")
        text = r.to_coordinator_text()
        assert "security" in text
        assert "0" in text
        assert "No issues found" in text

    def test_to_coordinator_text_with_issues(self):
        from app.agent import SpecialistResult
        r = SpecialistResult(
            name="logic",
            comments=[{"path": "a.py", "line": 5, "risk": "HIGH", "raw_body": "Crash here."}],
            issue_count=1,
            highest_risk="HIGH",
            summary="Found a crash.",
        )
        text = r.to_coordinator_text()
        assert "HIGH" in text
        assert "Found a crash." in text
        assert "a.py:5" in text

    def test_to_coordinator_text_timed_out(self):
        from app.agent import SpecialistResult
        r = SpecialistResult(name="performance", timed_out=True)
        text = r.to_coordinator_text()
        assert "TIMED OUT" in text
