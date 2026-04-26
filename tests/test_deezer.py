import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from util import arun

from streamrip.client.deezer import DeezerClient
from streamrip.client.downloadable import DeezerDownloadable
from streamrip.config import Config
from streamrip.exceptions import NonStreamableError


@pytest.fixture(scope="session")
def deezer_client():
    """Integration test fixture - requires DEEZER_ARL environment variable"""
    config = Config.defaults()
    config.session.deezer.arl = os.environ.get("DEEZER_ARL", "")
    config.session.deezer.quality = 2  # FLAC
    config.session.deezer.lower_quality_if_not_available = True
    client = DeezerClient(config)
    arun(client.login())

    yield client

    arun(client.session.close())


@pytest.fixture
def mock_deezer_client():
    """Unit test fixture - mocked client for fast testing"""
    config = Config.defaults()
    config.session.deezer.arl = "test_arl"
    config.session.deezer.quality = 2
    config.session.deezer.lower_quality_if_not_available = True

    client = DeezerClient(config)
    client.client = Mock()
    client.client.gw = Mock()
    client.session = Mock()

    return client


# ===== UNIT TESTS =====


def test_deezer_fallback_logic_with_mock_data(mock_deezer_client):
    """Unit test: fallback logic works with mocked track data"""
    # Mock track info where FLAC is unavailable but MP3_320 is available
    # quality_map: [(9, "MP3_128"), (3, "MP3_320"), (1, "FLAC")]  # noqa: ERA001
    # So FILESIZE_MP3_128 = quality 0, FILESIZE_MP3_320 = quality 1, FILESIZE_FLAC = quality 2
    mock_track_info = {
        "FILESIZE_FLAC": 0,  # FLAC unavailable (quality 2)
        "FILESIZE_MP3_320": 5000000,  # MP3_320 available (quality 1)
        "FILESIZE_MP3_128": 2000000,  # MP3_128 available (quality 0)
        "TRACK_TOKEN": "test_token",
    }

    # Mock the client methods
    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = "https://test.mp3"

    # Test fallback behavior
    with patch.object(mock_deezer_client, "get_session"):
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

        # Should have fallen back to quality 1 (MP3_320) since FLAC is unavailable
        assert downloadable.quality == 1


def test_deezer_no_fallback_when_quality_available(mock_deezer_client):
    """Unit test: no fallback when requested quality is available"""
    # Mock track info where FLAC is available
    # quality_map: [(9, "MP3_128"), (3, "MP3_320"), (1, "FLAC")]  # noqa: ERA001
    mock_track_info = {
        "FILESIZE_FLAC": 25000000,  # FLAC available (quality 2)
        "FILESIZE_MP3_320": 5000000,  # MP3_320 available (quality 1)
        "FILESIZE_MP3_128": 2000000,  # MP3_128 available (quality 0)
        "TRACK_TOKEN": "test_token",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = "https://test.flac"

    with patch.object(mock_deezer_client, "get_session"):
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

        # Should use requested quality 2 (FLAC)
        assert downloadable.quality == 2


def test_deezer_fallback_to_lowest_available_quality(mock_deezer_client):
    """Unit test: fallback walks down quality list until finding available quality"""
    # Mock track info where only MP3_128 is available
    # quality_map: [(9, "MP3_128"), (3, "MP3_320"), (1, "FLAC")]  # noqa: ERA001
    mock_track_info = {
        "FILESIZE_FLAC": 0,  # FLAC unavailable (quality 2)
        "FILESIZE_MP3_320": 0,  # MP3_320 unavailable (quality 1)
        "FILESIZE_MP3_128": 2000000,  # MP3_128 available (quality 0)
        "TRACK_TOKEN": "test_token",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = "https://test.mp3"

    with patch.object(mock_deezer_client, "get_session"):
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

        # Should have fallen back to quality 0 (MP3_128) since higher qualities unavailable
        assert downloadable.quality == 0


def test_deezer_no_fallback_when_disabled(mock_deezer_client):
    """Unit test: no fallback when lower_quality_if_not_available is False"""
    # Disable fallback
    mock_deezer_client.config.lower_quality_if_not_available = False

    # Mock track info where FLAC is unavailable
    # quality_map: [(9, "MP3_128"), (3, "MP3_320"), (1, "FLAC")]  # noqa: ERA001
    mock_track_info = {
        "FILESIZE_FLAC": 0,  # FLAC unavailable (quality 2)
        "FILESIZE_MP3_320": 5000000,  # MP3_320 available (quality 1)
        "FILESIZE_MP3_128": 2000000,  # MP3_128 available (quality 0)
        "TRACK_TOKEN": "test_url",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = "https://test.mp3"

    # Should raise an error when requested quality is unavailable and fallback is disabled
    with patch.object(mock_deezer_client, "get_session"):
        with pytest.raises(
            NonStreamableError,
            match="The requested quality 2 is not available and fallback is disabled",
        ):
            arun(mock_deezer_client.get_downloadable("123", quality=2))


def test_deezer_raises_actionable_error_on_track_lookup_failure(mock_deezer_client):
    mock_deezer_client.client.gw.get_track.side_effect = RuntimeError("gateway timeout")

    with pytest.raises(
        NonStreamableError, match="Unable to query Deezer track metadata"
    ):
        arun(mock_deezer_client.get_downloadable("123", quality=2))


def test_deezer_falls_back_to_encrypted_url_when_media_api_returns_none(
    mock_deezer_client,
):
    mock_track_info = {
        "FILESIZE_FLAC": 25000000,
        "FILESIZE_MP3_320": 5000000,
        "FILESIZE_MP3_128": 2000000,
        "TRACK_TOKEN": "test_token",
        "MD5_ORIGIN": "abcdef0123456789abcdef0123456789",
        "MEDIA_VERSION": "1",
    }

    mock_deezer_client.client.gw.get_track.return_value = mock_track_info
    mock_deezer_client.client.get_track_url.return_value = None

    with patch.object(
        mock_deezer_client,
        "_get_encrypted_file_url",
        return_value="https://e-cdns-proxy-a.dzcdn.net/mobile/1/abc",
    ) as encrypted_url:
        downloadable = arun(mock_deezer_client.get_downloadable("123", quality=2))

    encrypted_url.assert_called_once()
    assert downloadable.url.startswith("https://e-cdns-proxy")


@pytest.mark.asyncio
async def test_deezer_downloadable_encrypted_stream_does_not_buffer_whole_file(
    tmp_path: Path,
):
    class _FakeContent:
        def __init__(self, chunks):
            self._chunks = chunks

        async def iter_chunks(self):
            for chunk in self._chunks:
                yield chunk, True

    class _FakeResponse:
        def __init__(self, chunks):
            self.headers = {"Content-Length": str(sum(len(c) for c in chunks))}
            self.content = _FakeContent(chunks)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

    class _FakeSession:
        def __init__(self, chunks):
            self._chunks = chunks
            self.headers = {}

        def get(self, *_args, **_kwargs):
            return _FakeResponse(self._chunks)

    chunks = [
        b"A" * 2048,
        b"B" * 2048,
        b"C" * 2048,
        b"D" * 16000,
    ]
    info = {
        "url": "https://e-cdns-proxy-a.dzcdn.net/mobile/1/abc",
        "id": 123,
        "quality": 2,
        "quality_to_size": [0, 0, 22144],
    }
    downloadable = DeezerDownloadable(_FakeSession(chunks), info)
    callback_calls: list[int] = []
    output_path = tmp_path / "out.bin"

    with patch.object(
        DeezerDownloadable,
        "_decrypt_chunk",
        side_effect=lambda _key, data: data,
    ) as decrypt:
        await downloadable._download(str(output_path), callback_calls.append)

    assert output_path.read_bytes() == b"".join(chunks)
    assert sum(callback_calls) == len(b"".join(chunks))
    assert len(callback_calls) >= 2
    assert decrypt.call_count == 4


@pytest.mark.asyncio
async def test_deezer_downloadable_encrypted_stream_small_tail(
    tmp_path: Path,
):
    """Tail shorter than 2048 bytes must be written raw (no decrypt call)."""

    class _FakeContent:
        def __init__(self, chunks):
            self._chunks = chunks

        async def iter_chunks(self):
            for chunk in self._chunks:
                yield chunk, True

    class _FakeResponse:
        def __init__(self, chunks):
            self.headers = {"Content-Length": str(sum(len(c) for c in chunks))}
            self.content = _FakeContent(chunks)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

    class _FakeSession:
        def __init__(self, chunks):
            self._chunks = chunks
            self.headers = {}

        def get(self, *_args, **_kwargs):
            return _FakeResponse(self._chunks)

    # 4 full 6144-byte groups plus a 1024-byte tail which is below 2048 and must
    # be written raw (no decrypt call for the tail).  Total > 20 000 bytes avoids
    # the small-file JSON-error probe in DeezerDownloadable._download.
    chunks = [
        b"A" * 6144,
        b"B" * 6144,
        b"C" * 6144,
        b"D" * 6144,
        b"E" * 1024,
    ]
    info = {
        "url": "https://e-cdns-proxy-a.dzcdn.net/mobile/1/abc",
        "id": 456,
        "quality": 2,
        "quality_to_size": [0, 0, 25600],
    }
    downloadable = DeezerDownloadable(_FakeSession(chunks), info)
    callback_calls: list[int] = []
    output_path = tmp_path / "out_small_tail.bin"

    with patch.object(
        DeezerDownloadable,
        "_decrypt_chunk",
        side_effect=lambda _key, data: data,
    ) as decrypt:
        await downloadable._download(str(output_path), callback_calls.append)

    assert output_path.read_bytes() == b"".join(chunks)
    assert sum(callback_calls) == len(b"".join(chunks))
    # 4 full 6144-byte groups → 4 decrypt calls; the 1024-byte tail is raw
    assert decrypt.call_count == 4


# ===== INTEGRATION TEST =====


@pytest.mark.skipif(
    "DEEZER_ARL" not in os.environ, reason="Deezer ARL not found in env."
)
def test_deezer_fallback_actually_occurred(deezer_client):
    """Integration test: verify fallback works with real track 77874822"""
    # We know track 77874822 doesn't have FLAC available, so test fallback scenario
    downloadable = arun(deezer_client.get_downloadable("77874822", quality=2))

    # Since we requested FLAC (quality=2) but it's not available,
    # we should have fallen back to the next available quality (1 = MP3_320)
    assert (
        downloadable.quality == 1
    ), "Should have fallen back to MP3_320 when FLAC unavailable"
    print("Fallback occurred: FLAC unavailable, fell back to MP3_320")

    # Verify the URL is actually accessible and working
    assert downloadable.url.startswith("https://")
    assert downloadable._size > 0, "Downloadable should have a valid file size"
    assert downloadable.extension == "mp3", "MP3_320 should have .mp3 extension"
