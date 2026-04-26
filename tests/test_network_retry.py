import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from streamrip.utils.network import aiohttp_call_with_retry


@pytest.mark.asyncio
async def test_aiohttp_call_with_retry_retries_timeout_then_succeeds():
    calls = 0

    async def _call():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.TimeoutError()
        return "ok"

    with patch("asyncio.sleep", new=AsyncMock()) as mocked_sleep:
        result = await aiohttp_call_with_retry(
            _call,
            operation="test_timeout",
            attempts=2,
            delay_seconds=0.01,
        )

    assert result == "ok"
    assert calls == 2
    mocked_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_aiohttp_call_with_retry_retries_result_then_returns_last():
    calls = 0

    async def _call():
        nonlocal calls
        calls += 1
        return {"status": 503 if calls == 1 else 200}

    with patch("asyncio.sleep", new=AsyncMock()) as mocked_sleep:
        result = await aiohttp_call_with_retry(
            _call,
            operation="test_result",
            attempts=2,
            delay_seconds=0.01,
            should_retry_result=lambda payload: (
                payload["status"] == 503,
                "status_503",
            ),
        )

    assert result == {"status": 200}
    assert calls == 2
    mocked_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_aiohttp_call_with_retry_raises_terminal_timeout():
    async def _call():
        raise asyncio.TimeoutError()

    with patch("asyncio.sleep", new=AsyncMock()) as mocked_sleep:
        with pytest.raises(asyncio.TimeoutError):
            await aiohttp_call_with_retry(
                _call,
                operation="test_terminal",
                attempts=2,
                delay_seconds=0.01,
            )
    mocked_sleep.assert_awaited_once()
