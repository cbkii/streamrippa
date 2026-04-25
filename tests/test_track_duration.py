from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.media.track import DownloadError, Track


@pytest.mark.asyncio
async def test_track_duration_validation_rejects_short_preview(tmp_path):
    db = MagicMock()
    db.set_failed = MagicMock()
    db.clear_failed = MagicMock()
    db.set_downloaded = MagicMock()

    track = Track(
        meta=SimpleNamespace(
            title="Song",
            artist="Artist",
            info=SimpleNamespace(id="123"),
        ),
        downloadable=SimpleNamespace(source="deezer"),
        config=MagicMock(),
        folder=str(tmp_path),
        cover_path=None,
        db=db,
        download_path=str(tmp_path / "song.mp3"),
        expected_duration_ms=240000,
    )
    (tmp_path / "song.mp3").write_bytes(b"fake")
    track._convert = AsyncMock()

    fake_info = SimpleNamespace(info=SimpleNamespace(length=30.0))
    with patch("mutagen.File", return_value=fake_info):
        with pytest.raises(DownloadError):
            await track._validate_expected_duration()

    db.set_failed.assert_called_once()


@pytest.mark.asyncio
async def test_track_duration_validation_accepts_close_length(tmp_path):
    db = MagicMock()
    track = Track(
        meta=SimpleNamespace(
            title="Song",
            artist="Artist",
            info=SimpleNamespace(id="123"),
        ),
        downloadable=SimpleNamespace(source="qobuz"),
        config=MagicMock(),
        folder=str(tmp_path),
        cover_path=None,
        db=db,
        download_path=str(tmp_path / "song.flac"),
        expected_duration_ms=240000,
    )
    (tmp_path / "song.flac").write_bytes(b"fake")
    fake_info = SimpleNamespace(info=SimpleNamespace(length=239.0))
    with patch("mutagen.File", return_value=fake_info):
        await track._validate_expected_duration()
    db.set_failed.assert_not_called()
