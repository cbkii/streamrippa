import asyncio
import logging
from unittest.mock import AsyncMock

import aiohttp
import pytest

from streamrip.client.tidal import TidalClient
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TypeError("bad lyrics payload"),
        NonStreamableError("lyrics unavailable"),
        aiohttp.ClientError("lyrics request failed"),
        asyncio.TimeoutError(),
    ],
)
async def test_get_metadata_ignores_track_lyrics_errors(error, caplog):
    client = TidalClient(Config.defaults())
    client._api_request = AsyncMock(
        side_effect=[
            {"id": "123", "title": "Track"},
            error,
        ]
    )

    with caplog.at_level(logging.WARNING, logger="streamrip"):
        result = await client.get_metadata("123", "track")

    assert result == {"id": "123", "title": "Track"}
    assert client._api_request.await_count == 2
    assert "Failed to get lyrics for 123" in caplog.text
