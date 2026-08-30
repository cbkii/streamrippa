from __future__ import annotations

from http.client import HTTPException
from unittest.mock import AsyncMock

import pytest
import requests

from streamrip.client import downloadable as downloadable_module
from streamrip.client.downloadable import BasicDownloadable, _is_excessive_headers_error


def _wrapped_excessive_headers_error() -> requests.ConnectionError:
    return requests.ConnectionError(
        "Connection aborted.",
        HTTPException("got more than 100 headers"),
    )


def test_excessive_headers_classifier_handles_requests_wrapper() -> None:
    assert _is_excessive_headers_error(_wrapped_excessive_headers_error())


def test_excessive_headers_classifier_rejects_unrelated_connection_error() -> None:
    assert not _is_excessive_headers_error(
        requests.ConnectionError("Connection reset by peer")
    )


@pytest.mark.asyncio
async def test_basic_downloadable_falls_back_to_aiohttp_for_excessive_headers(
    monkeypatch,
) -> None:
    fast = AsyncMock(side_effect=_wrapped_excessive_headers_error())
    fallback = AsyncMock(return_value=None)
    monkeypatch.setattr(downloadable_module, "fast_async_download", fast)
    monkeypatch.setattr(downloadable_module, "_aiohttp_stream_download", fallback)

    session = AsyncMock()
    session.headers = {"User-Agent": "test"}
    downloadable = BasicDownloadable(
        session,
        "https://example.invalid/audio.flac",
        "flac",
        source="qobuz",
    )

    # Keep one stable callback object so both transfer paths can be asserted.
    def callback(_size: int) -> None:
        return None

    await downloadable._download("/tmp/test.flac", callback)

    fast.assert_awaited_once_with(
        "/tmp/test.flac",
        "https://example.invalid/audio.flac",
        session.headers,
        callback,
    )
    fallback.assert_awaited_once_with(
        "/tmp/test.flac",
        "https://example.invalid/audio.flac",
        session,
        callback,
    )


@pytest.mark.asyncio
async def test_basic_downloadable_does_not_mask_other_transfer_errors(
    monkeypatch,
) -> None:
    error = requests.ConnectionError("Connection reset by peer")
    fast = AsyncMock(side_effect=error)
    fallback = AsyncMock(return_value=None)
    monkeypatch.setattr(downloadable_module, "fast_async_download", fast)
    monkeypatch.setattr(downloadable_module, "_aiohttp_stream_download", fallback)

    session = AsyncMock()
    session.headers = {}
    downloadable = BasicDownloadable(
        session,
        "https://example.invalid/audio.flac",
        "flac",
        source="qobuz",
    )

    with pytest.raises(requests.ConnectionError, match="reset by peer"):
        await downloadable._download("/tmp/test.flac", lambda _size: None)

    fallback.assert_not_awaited()
