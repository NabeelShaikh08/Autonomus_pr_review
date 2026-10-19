"""Tests for GitHub API interactions."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_resp(status_code: int, json_data=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or []
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


class TestGetPrFiles:
    @pytest.mark.asyncio
    async def test_returns_files_single_page(self):
        from app.github import get_pr_files

        files = [{"filename": "src/app.py", "status": "modified", "patch": "+x"}]
        resp = _make_resp(200, files)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await get_pr_files("owner", "repo", 1, "token")

        assert len(result) == 1
        assert result[0]["filename"] == "src/app.py"

    @pytest.mark.asyncio
    async def test_paginates_until_empty(self):
        from app.github import get_pr_files

        page1 = [{"filename": f"file{i}.py", "status": "modified", "patch": "+"} for i in range(30)]
        page2 = [{"filename": "last.py", "status": "modified", "patch": "+"}]
        resp1 = _make_resp(200, page1)
        resp2 = _make_resp(200, page2)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[resp1, resp2])
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await get_pr_files("owner", "repo", 1, "token")

        assert len(result) == 31

    @pytest.mark.asyncio
    async def test_retries_on_5xx(self):
        from app.github import get_pr_files

        err_resp = _make_resp(503, text="Service Unavailable")
        ok_resp = _make_resp(200, [{"filename": "a.py", "status": "modified", "patch": "+"}])

        with patch("httpx.AsyncClient") as mock_cls, \
             patch("asyncio.sleep", return_value=None):
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=[err_resp, ok_resp])
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await get_pr_files("owner", "repo", 1, "token")

        assert len(result) == 1


class TestPostReview:
    @pytest.mark.asyncio
    async def test_posts_review_successfully(self):
        from app.github import post_review

        resp = _make_resp(201)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_review(
                owner="owner",
                repo="repo",
                pr_number=1,
                commit_sha="abc123",
                token="token",
                summary="## Review\nLooks good.",
                comments=[{"path": "src/a.py", "line": 5, "body": "Fix this."}],
            )

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs["json"]
        assert payload["commit_id"] == "abc123"
        assert len(payload["comments"]) == 1

    @pytest.mark.asyncio
    async def test_retries_on_5xx(self):
        from app.github import post_review

        err_resp = _make_resp(502, text="Bad Gateway")
        ok_resp = _make_resp(201)

        with patch("httpx.AsyncClient") as mock_cls, \
             patch("asyncio.sleep", return_value=None):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=[err_resp, ok_resp])
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_review(
                owner="o", repo="r", pr_number=1,
                commit_sha="sha", token="t",
                summary="ok", comments=[],
            )

        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_moves_invalid_comments_to_overflow(self):
        from app.github import post_review

        resp = _make_resp(201)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await post_review(
                owner="o", repo="r", pr_number=1,
                commit_sha="sha", token="t",
                summary="Summary.",
                comments=[
                    {"path": "src/a.py", "line": 5, "body": "Valid comment."},
                    {"path": "", "line": None, "body": "No path or line."},
                ],
            )

        payload = mock_client.post.call_args.kwargs["json"]
        # Only the valid comment goes inline
        assert len(payload["comments"]) == 1
        # The invalid one appears in the body
        assert "No path or line." in payload["body"]
