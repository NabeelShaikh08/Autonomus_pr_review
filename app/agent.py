"""
Claude agentic loop for PR code review.

Claude is given two tools:
  - add_review_comment  : flag an issue on a specific file + line
  - set_review_summary  : write the overall PR summary (called once, at the end)

The loop runs until Claude calls set_review_summary (end signal) or hits
the max-iteration guard.
"""

import logging
from typing import Any

import anthropic

from app.config import get_settings

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 4096
_MAX_ITERATIONS = 10   # Guard against infinite tool-use loops

_SYSTEM_PROMPT = """\
You are an expert code reviewer. You will be given a pull request diff.

Your job:
1. Read the diff carefully.
2. For every genuine issue you find, call `add_review_comment` with the exact \
filename, the line number (from the diff), and a concise, actionable comment.
3. When you have reviewed everything, call `set_review_summary` once with a \
short overall assessment.

Guidelines:
- Only flag real issues: bugs, security vulnerabilities, performance problems, \
logic errors, or missing error handling.
- Do NOT flag style nitpicks, formatting, or personal preferences.
- Do NOT comment on deleted lines (only additions matter).
- Be specific and constructive. Explain WHY something is a problem and HOW to fix it.
- If the PR looks good, say so in the summary and post no inline comments.
- Use the line number exactly as it appears in the diff (the number after +).
- Risk levels: LOW (minor improvement), MEDIUM (could cause issues), HIGH (bug/security).
"""

_TOOLS: list[dict] = [
    {
        "name": "add_review_comment",
        "description": (
            "Flag an issue on a specific line of a specific file in the PR diff. "
            "Call this once per issue found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path exactly as shown in the diff header.",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number in the NEW file (right side of diff).",
                },
                "risk": {
                    "type": "string",
                    "enum": ["LOW", "MEDIUM", "HIGH"],
                    "description": "Severity of the issue.",
                },
                "body": {
                    "type": "string",
                    "description": "Concise, actionable review comment (markdown supported).",
                },
            },
            "required": ["path", "line", "risk", "body"],
        },
    },
    {
        "name": "set_review_summary",
        "description": (
            "Write the overall PR review summary. "
            "Call this ONCE after you have added all inline comments. "
            "This signals that the review is complete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Overall assessment of the PR (markdown supported).",
                },
                "verdict": {
                    "type": "string",
                    "enum": ["APPROVED", "NEEDS_WORK", "CRITICAL"],
                    "description": (
                        "APPROVED: looks good. "
                        "NEEDS_WORK: minor issues. "
                        "CRITICAL: serious bugs or security issues."
                    ),
                },
            },
            "required": ["summary", "verdict"],
        },
    },
]


def _verdict_emoji(verdict: str) -> str:
    return {"APPROVED": "✅", "NEEDS_WORK": "⚠️", "CRITICAL": "🚨"}.get(verdict, "")


def _format_summary(summary: str, verdict: str) -> str:
    emoji = _verdict_emoji(verdict)
    return (
        f"## PR Review {emoji} {verdict}\n\n"
        f"{summary}\n\n"
        f"---\n*Reviewed by [PR Reviewer](https://github.com/NabeelShaikh08/prreview)*"
    )


def _format_comment(risk: str, body: str) -> str:
    risk_icons = {"LOW": "💡", "MEDIUM": "⚠️", "HIGH": "🚨"}
    icon = risk_icons.get(risk, "💬")
    return f"{icon} **{risk}**\n\n{body}"


async def run_review(
    pr_title: str,
    pr_body: str,
    diff_text: str,
) -> tuple[str, list[dict]]:
    """
    Run the Claude agentic review loop.

    Returns:
        summary  : formatted overall review text
        comments : list of {path, line, body} dicts for inline posting
    """
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    user_message = (
        f"**PR Title:** {pr_title}\n\n"
        f"**PR Description:**\n{pr_body or '_No description provided._'}\n\n"
        f"**Diff:**\n\n{diff_text}"
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

    collected_comments: list[dict] = []
    summary_text = "_No summary provided._"
    iterations = 0

    while iterations < _MAX_ITERATIONS:
        iterations += 1
        logger.debug("Claude iteration %d", iterations)

        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            tools=_TOOLS,
            messages=messages,
        )

        # Collect tool calls from this response
        tool_results: list[dict[str, Any]] = []
        review_done = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name: str = block.name
            tool_input: dict = block.input

            if tool_name == "add_review_comment":
                comment = {
                    "path": tool_input["path"],
                    "line": tool_input["line"],
                    "body": _format_comment(tool_input["risk"], tool_input["body"]),
                }
                collected_comments.append(comment)
                logger.debug(
                    "Comment queued: %s line %d [%s]",
                    tool_input["path"],
                    tool_input["line"],
                    tool_input["risk"],
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Comment recorded.",
                })

            elif tool_name == "set_review_summary":
                summary_text = _format_summary(
                    tool_input["summary"], tool_input["verdict"]
                )
                review_done = True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Summary recorded. Review complete.",
                })

        # Feed all tool results back in one turn
        if tool_results:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        if review_done or response.stop_reason == "end_turn":
            break

    if iterations >= _MAX_ITERATIONS:
        logger.warning("Review hit max iterations (%d); stopping early", _MAX_ITERATIONS)

    logger.info(
        "Review complete: %d comments, %d iterations",
        len(collected_comments),
        iterations,
    )
    return summary_text, collected_comments
