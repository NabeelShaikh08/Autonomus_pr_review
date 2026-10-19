"""Tests for file filtering logic."""

import pytest
from unittest.mock import patch
from app.filters import filter_files, build_diff_text


MOCK_SETTINGS = {
    "github_app_id": "1",
    "github_private_key": "key",
    "github_webhook_secret": "secret",
    "groq_api_key": "key",
    "max_files_per_review": 3,
    "max_patch_chars": 100,
    "ignored_patterns": ["package-lock.json", "vendor/", "*.min.js"],
}


def make_file(filename, status="modified", patch="@@ -1,3 +1,4 @@\n+new line"):
    return {"filename": filename, "status": status, "patch": patch}


class TestFilterFiles:
    def test_keeps_normal_files(self):
        files = [make_file("src/main.py")]
        result = filter_files(files)
        assert len(result) == 1

    def test_skips_deleted_files(self):
        files = [make_file("src/old.py", status="removed")]
        result = filter_files(files)
        assert result == []

    def test_skips_binary_files(self):
        files = [{"filename": "image.png", "status": "modified", "patch": None}]
        result = filter_files(files)
        assert result == []

    def test_skips_ignored_patterns(self):
        files = [
            make_file("package-lock.json"),
            make_file("vendor/lib/code.js"),
            make_file("app.min.js"),
            make_file("src/app.py"),
        ]
        with patch("app.filters.get_settings") as mock:
            mock.return_value.ignored_patterns = MOCK_SETTINGS["ignored_patterns"]
            mock.return_value.max_files_per_review = 50
            result = filter_files(files)

        filenames = [f["filename"] for f in result]
        assert "package-lock.json" not in filenames
        assert "vendor/lib/code.js" not in filenames
        assert "app.min.js" not in filenames
        assert "src/app.py" in filenames

    def test_caps_at_max_files(self):
        files = [make_file(f"src/file{i}.py") for i in range(10)]
        with patch("app.filters.get_settings") as mock:
            mock.return_value.ignored_patterns = []
            mock.return_value.max_files_per_review = 3
            result = filter_files(files)

        assert len(result) == 3


class TestBuildDiffText:
    def test_basic_output(self):
        files = [make_file("src/app.py", patch="@@ -1 +1 @@\n+hello")]
        with patch("app.filters.get_settings") as mock:
            mock.return_value.max_patch_chars = 10_000
            result, truncated = build_diff_text(files)

        assert "src/app.py" in result
        assert "+hello" in result
        assert truncated is False

    def test_truncates_large_diffs(self):
        patch_text = "x" * 200
        files = [make_file("src/big.py", patch=patch_text)]
        with patch("app.filters.get_settings") as mock:
            mock.return_value.max_patch_chars = 100
            result, truncated = build_diff_text(files)

        assert "truncated" in result
        assert truncated is True
        assert len(result) <= 200
