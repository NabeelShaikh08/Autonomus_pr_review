"""
Decide which changed files are worth reviewing.

Rules (applied in order):
  1. Skip files whose path matches any ignored pattern (vendor, locks, generated, etc.)
  2. Skip binary files (no patch in the GitHub API response)
  3. Skip deleted files (nothing new to review)
  4. Cap the total number of files sent to Claude
  5. Truncate the combined patch if it exceeds the character budget
"""

import fnmatch
import logging
from typing import TypedDict

from app.config import get_settings

logger = logging.getLogger(__name__)


class PRFile(TypedDict):
    filename: str
    status: str          # added | modified | removed | renamed
    patch: str | None    # unified diff hunk; None for binary files


def _is_ignored(filename: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        # Match against the full path and the basename
        if fnmatch.fnmatch(filename, pattern):
            return True
        if fnmatch.fnmatch(filename.split("/")[-1], pattern):
            return True
        # Directory prefix match  (e.g. "vendor/" matches "vendor/foo/bar.js")
        if pattern.endswith("/") and filename.startswith(pattern):
            return True
    return False


def filter_files(files: list[PRFile]) -> list[PRFile]:
    """Return the subset of files that should be reviewed."""
    settings = get_settings()
    reviewable: list[PRFile] = []

    for f in files:
        filename = f["filename"]

        if f["status"] == "removed":
            logger.debug("Skipping deleted file: %s", filename)
            continue

        if f.get("patch") is None:
            logger.debug("Skipping binary/no-patch file: %s", filename)
            continue

        if _is_ignored(filename, settings.ignored_patterns):
            logger.debug("Skipping ignored file: %s", filename)
            continue

        reviewable.append(f)

    if len(reviewable) > settings.max_files_per_review:
        logger.warning(
            "PR has %d reviewable files; capping at %d",
            len(reviewable),
            settings.max_files_per_review,
        )
        reviewable = reviewable[: settings.max_files_per_review]

    return reviewable


def build_diff_text(files: list[PRFile]) -> tuple[str, bool]:
    """
    Concatenate all patches into a single diff string for the AI.
    Truncates at max_patch_chars to stay within token budget.

    Returns:
        (diff_text, was_truncated)
    """
    settings = get_settings()
    parts: list[str] = []

    for f in files:
        parts.append(f"### {f['filename']} ({f['status']})\n```diff\n{f['patch']}\n```")

    combined = "\n\n".join(parts)

    if len(combined) > settings.max_patch_chars:
        logger.warning(
            "Diff is %d chars; truncating to %d",
            len(combined),
            settings.max_patch_chars,
        )
        combined = combined[: settings.max_patch_chars]
        combined += "\n\n[diff truncated — PR is too large to review in full]"
        return combined, True

    return combined, False
