"""Tests for GitHub App authentication and token caching."""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestTokenCaching:
    def setup_method(self):
        # Clear the cache before each test
        import app.auth as auth_module
        auth_module._local_cache.clear()
        auth_module._token_locks.clear()

    @pytest.mark.asyncio
    async def test_returns_cached_token_when_valid(self):
        import app.auth as auth_module
        future_expiry = time.time() + 3600
        auth_module._local_cache[42] = ("cached-token", future_expiry)

        token = await auth_module.get_installation_token(42)
        assert token == "cached-token"

    @pytest.mark.asyncio
    async def test_fetches_new_token_when_cache_empty(self):
        import app.auth as auth_module

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "token": "new-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("app.auth._build_jwt", return_value="fake-jwt"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await auth_module.get_installation_token(99)

        assert token == "new-token"
        assert 99 in auth_module._local_cache

    @pytest.mark.asyncio
    async def test_refreshes_token_near_expiry(self):
        import app.auth as auth_module
        # Set token expiring in 2 minutes (within the 5-minute buffer)
        soon_expiry = time.time() + 120
        auth_module._local_cache[7] = ("old-token", soon_expiry)

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "token": "refreshed-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("app.auth._build_jwt", return_value="fake-jwt"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            token = await auth_module.get_installation_token(7)

        assert token == "refreshed-token"

    @pytest.mark.asyncio
    async def test_raises_on_github_error(self):
        import app.auth as auth_module

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("Unauthorized"))

        with patch("app.auth._build_jwt", return_value="fake-jwt"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(Exception):
                await auth_module.get_installation_token(55)

    @pytest.mark.asyncio
    async def test_concurrent_requests_fetch_token_once(self):
        import app.auth as auth_module
        fetch_count = 0

        async def fake_post(*args, **kwargs):
            nonlocal fetch_count
            fetch_count += 1
            await asyncio.sleep(0.05)  # simulate network delay
            mock_resp = MagicMock()
            mock_resp.status_code = 201
            mock_resp.json.return_value = {
                "token": "concurrent-token",
                "expires_at": "2099-01-01T00:00:00Z",
            }
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        with patch("app.auth._build_jwt", return_value="fake-jwt"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = fake_post
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            results = await asyncio.gather(
                auth_module.get_installation_token(77),
                auth_module.get_installation_token(77),
                auth_module.get_installation_token(77),
            )

        assert all(r == "concurrent-token" for r in results)
        assert fetch_count == 1  # only one actual fetch despite 3 concurrent requests
