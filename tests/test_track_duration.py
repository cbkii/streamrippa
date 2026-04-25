from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


@pytest.mark.asyncio
async def test_track_duration_validation_accepts_hybrid_tolerance(tmp_path):
    db = MagicMock()
    config = MagicMock()
    config.session.csv_resolver.local_skip_duration_tolerance_ratio = 0.20
    config.session.csv_resolver.local_skip_duration_tolerance_seconds = 12
    track = Track(
        meta=SimpleNamespace(
            title="Song",
            artist="Artist",
            info=SimpleNamespace(id="123"),
        ),
        downloadable=SimpleNamespace(source="qobuz"),
        config=config,
        folder=str(tmp_path),
        cover_path=None,
        db=db,
        download_path=str(tmp_path / "song.flac"),
        expected_duration_ms=240000,
    )
    (tmp_path / "song.flac").write_bytes(b"fake")
    # 30s delta is accepted because ratio tolerance = 48s.
    fake_info = SimpleNamespace(info=SimpleNamespace(length=270.0))
    with patch("mutagen.File", return_value=fake_info):
        await track._validate_expected_duration()
    db.set_failed.assert_not_called()


@pytest.mark.asyncio
async def test_track_duration_validation_skipped_when_no_expected(tmp_path):
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
        expected_duration_ms=None,
    )
    (tmp_path / "song.flac").write_bytes(b"fake")
    with patch("mutagen.File") as mutagen_file:
        await track._validate_expected_duration()
    mutagen_file.assert_not_called()
    db.set_failed.assert_not_called()


@pytest.mark.asyncio
async def test_track_duration_validation_rejects_short_preview_even_with_tolerance(
    tmp_path,
):
    db = MagicMock()
    db.set_failed = MagicMock()
    config = MagicMock()
    config.session.csv_resolver.local_skip_duration_tolerance_ratio = 0.50
    config.session.csv_resolver.local_skip_duration_tolerance_seconds = 60
    track = Track(
        meta=SimpleNamespace(
            title="Song",
            artist="Artist",
            info=SimpleNamespace(id="123"),
        ),
        downloadable=SimpleNamespace(source="qobuz"),
        config=config,
        folder=str(tmp_path),
        cover_path=None,
        db=db,
        download_path=str(tmp_path / "song.flac"),
        expected_duration_ms=180000,
    )
    (tmp_path / "song.flac").write_bytes(b"fake")
    fake_info = SimpleNamespace(info=SimpleNamespace(length=30.0))
    with patch("mutagen.File", return_value=fake_info):
        with pytest.raises(DownloadError):
            await track._validate_expected_duration()
    db.set_failed.assert_called_once()
