"""
Groq agentic loop for PR code review.

Uses Llama 3.3 70B via Groq (free tier).
Groq's SDK is OpenAI-compatible — tool use works identically.

Claude is given two tools:
  - add_review_comment  : flag an issue on a specific file + line
  - set_review_summary  : write the overall PR summary (called once, at the end)

The loop runs until set_review_summary is called or the max-iteration guard fires.
"""

import logging
from groq import Groq

from app.config import get_settings

logger = logging.getLogger(__name__)

_MODEL = "llama-3.3-70b-versatile"
_MAX_TOKENS = 4096
_MAX_ITERATIONS = 10

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
- You MUST call set_review_summary at the end, always.
"""

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_review_comment",
            "description": (
                "Flag an issue on a specific line of a specific file in the PR diff. "
                "Call this once per issue found."
            ),
            "parameters": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "set_review_summary",
            "description": (
                "Write the overall PR review summary. "
                "Call this ONCE after you have added all inline comments. "
                "This signals that the review is complete."
            ),
            "parameters": {
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
    Run the Groq agentic review loop.

    Returns:
        summary  : formatted overall review text
        comments : list of {path, line, body} dicts for inline posting
    """
    import json

    settings = get_settings()
    client = Groq(api_key=settings.groq_api_key)

    user_message = (
        f"**PR Title:** {pr_title}\n\n"
        f"**PR Description:**\n{pr_body or '_No description provided._'}\n\n"
        f"**Diff:**\n\n{diff_text}"
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    collected_comments: list[dict] = []
    summary_text = "_Review completed._"
    iterations = 0

    while iterations < _MAX_ITERATIONS:
        iterations += 1
        logger.debug("Groq iteration %d", iterations)

        response = client.chat.completions.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            tools=_TOOLS,
            tool_choice="auto",
            messages=messages,
        )

        message = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        # Append assistant turn to history
        messages.append({"role": "assistant", "content": message.content or "", "tool_calls": message.tool_calls or []})

        # If no tool calls, model is done
        if not message.tool_calls:
            logger.debug("No tool calls — stopping loop")
            break

        review_done = False

        for tool_call in message.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                logger.warning("Could not parse tool args for %s", fn_name)
                fn_args = {}

            if fn_name == "add_review_comment":
                comment = {
                    "path": fn_args.get("path", ""),
                    "line": fn_args.get("line", 1),
                    "body": _format_comment(
                        fn_args.get("risk", "LOW"),
                        fn_args.get("body", ""),
                    ),
                }
                collected_comments.append(comment)
                logger.debug(
                    "Comment queued: %s line %d [%s]",
                    fn_args.get("path"),
                    fn_args.get("line"),
                    fn_args.get("risk"),
                )
                tool_result = "Comment recorded."

            elif fn_name == "set_review_summary":
                summary_text = _format_summary(
                    fn_args.get("summary", ""),
                    fn_args.get("verdict", "NEEDS_WORK"),
                )
                review_done = True
                tool_result = "Summary recorded. Review complete."

            else:
                tool_result = f"Unknown tool: {fn_name}"

            # Feed tool result back
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": tool_result,
            })

        if review_done or finish_reason == "stop":
            break

    if iterations >= _MAX_ITERATIONS:
        logger.warning("Review hit max iterations (%d); stopping early", _MAX_ITERATIONS)

    logger.info(
        "Review complete: %d comments, %d iterations",
        len(collected_comments),
        iterations,
    )
    return summary_text, collected_comments
