import asyncio
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
async def test_get_metadata_ignores_track_lyrics_errors(error):
    client = TidalClient(Config.defaults())
    client._api_request = AsyncMock(
        side_effect=[
            {"id": "123", "title": "Track"},
            error,
        ]
    )

    result = await client.get_metadata("123", "track")

    assert result == {"id": "123", "title": "Track"}
