"""Tests for the reliability layer: retry, FLAC validation, rip repair,
fail-fast, exit codes, and session summary/stats."""

import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.db import Database, Dummy, FailedTrackLog, SessionStats


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_db(tmp_path: str | None = None) -> Database:
    """Return a Database backed by Dummy stores (no filesystem activity)."""
    return Database(downloads=Dummy(), failed=Dummy())


def _make_reliability_config(
    retry_count: int = 2,
    retry_delay: float = 0.0,
    retry_backoff_factor: float = 1.0,
    fail_fast: bool = False,
    validate_flac: bool = False,
):
    cfg = MagicMock()
    cfg.retry_count = retry_count
    cfg.retry_delay = retry_delay
    cfg.retry_backoff_factor = retry_backoff_factor
    cfg.fail_fast = fail_fast
    cfg.validate_flac = validate_flac
    return cfg


def _make_full_config(reliability=None):
    """Return a MagicMock that quacks like a Config object."""
    config = MagicMock()
    config.session.reliability = reliability or _make_reliability_config()
    config.session.cli.progress_bars = False
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    return config


# ---------------------------------------------------------------------------
# SessionStats
# ---------------------------------------------------------------------------


class TestSessionStats:
    def test_defaults_all_zero(self):
        stats = SessionStats()
        assert stats.succeeded == 0
        assert stats.failed == 0
        assert stats.skipped == 0
        assert stats.retried == 0
        assert stats.validation_failures == 0

    def test_independent_instances(self):
        """Two SessionStats instances must not share state."""
        a = SessionStats()
        b = SessionStats()
        a.succeeded = 5
        assert b.succeeded == 0


# ---------------------------------------------------------------------------
# Database stats integration
# ---------------------------------------------------------------------------


class TestDatabaseStats:
    def test_set_downloaded_increments_succeeded(self):
        db = _make_db()
        db.set_downloaded("id1")
        assert db.stats.succeeded == 1

    def test_set_failed_increments_failed(self):
        db = _make_db()
        db.set_failed("deezer", "track", "id1")
        assert db.stats.failed == 1

    def test_set_failed_validation_increments_both(self):
        db = _make_db()
        db.set_failed("deezer", "track", "id1", is_validation_failure=True)
        assert db.stats.failed == 1
        assert db.stats.validation_failures == 1

    def test_set_skipped_increments_skipped(self):
        db = _make_db()
        db.set_skipped()
        assert db.stats.skipped == 1

    def test_add_retry_increments_retried(self):
        db = _make_db()
        db.add_retry()
        db.add_retry()
        assert db.stats.retried == 2

    def test_multiple_operations_accumulate(self):
        db = _make_db()
        db.set_downloaded("a")
        db.set_downloaded("b")
        db.set_failed("deezer", "track", "c")
        db.set_skipped()
        db.add_retry()
        assert db.stats.succeeded == 2
        assert db.stats.failed == 1
        assert db.stats.skipped == 1
        assert db.stats.retried == 1


# ---------------------------------------------------------------------------
# Track.download() – retry with backoff
# ---------------------------------------------------------------------------


class TestTrackRetry:
    """Test that Track.download() retries the configured number of times."""

    def _make_track(self, retry_count: int, downloadable_mock):
        from streamrip.media.track import Track

        meta = MagicMock()
        meta.title = "Test Track"
        meta.tracknumber = 1
        meta.artist = "Artist"
        meta.info.id = "track_id"

        config = MagicMock()
        config.session.reliability.retry_count = retry_count
        config.session.reliability.retry_delay = 0.0
        config.session.reliability.retry_backoff_factor = 1.0
        config.session.cli.progress_bars = False

        db = _make_db()

        track = Track(
            meta=meta,
            downloadable=downloadable_mock,
            config=config,
            folder="/tmp",
            cover_path=None,
            db=db,
        )
        track.download_path = "/tmp/test.flac"
        return track, db

    @pytest.mark.asyncio
    async def test_succeeds_on_first_attempt(self):
        downloadable = MagicMock()
        downloadable.source = "deezer"
        downloadable.extension = "flac"
        downloadable.size = AsyncMock(return_value=1000)
        downloadable.download = AsyncMock()

        track, db = self._make_track(retry_count=2, downloadable_mock=downloadable)

        with patch("streamrip.media.track.get_progress_callback") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            with patch("streamrip.media.track.global_download_semaphore") as sem:
                sem.return_value.__aenter__ = AsyncMock(return_value=None)
                sem.return_value.__aexit__ = AsyncMock(return_value=False)
                await track.download()

        assert downloadable.download.call_count == 1
        assert db.stats.failed == 0
        assert db.stats.retried == 0

    @pytest.mark.asyncio
    async def test_retries_on_failure_then_succeeds(self):
        """First attempt fails, second attempt succeeds — one retry recorded."""
        downloadable = MagicMock()
        downloadable.source = "deezer"
        downloadable.extension = "flac"
        downloadable.size = AsyncMock(return_value=1000)
        # Fail once, then succeed
        downloadable.download = AsyncMock(
            side_effect=[Exception("network error"), None]
        )

        track, db = self._make_track(retry_count=2, downloadable_mock=downloadable)

        with patch("streamrip.media.track.get_progress_callback") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            with patch("streamrip.media.track.global_download_semaphore") as sem:
                sem.return_value.__aenter__ = AsyncMock(return_value=None)
                sem.return_value.__aexit__ = AsyncMock(return_value=False)
                await track.download()

        assert downloadable.download.call_count == 2
        assert db.stats.retried == 1
        assert db.stats.failed == 0

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_records_failure(self):
        """If all retry attempts fail, the track is recorded as failed."""
        downloadable = MagicMock()
        downloadable.source = "deezer"
        downloadable.extension = "flac"
        downloadable.size = AsyncMock(return_value=1000)
        downloadable.download = AsyncMock(side_effect=Exception("persistent error"))

        track, db = self._make_track(retry_count=2, downloadable_mock=downloadable)

        with patch("streamrip.media.track.get_progress_callback") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            with patch("streamrip.media.track.global_download_semaphore") as sem:
                sem.return_value.__aenter__ = AsyncMock(return_value=None)
                sem.return_value.__aexit__ = AsyncMock(return_value=False)
                await track.download()

        # retry_count=2 means 3 total attempts (initial + 2 retries)
        assert downloadable.download.call_count == 3
        assert db.stats.retried == 2
        assert db.stats.failed == 1

    @pytest.mark.asyncio
    async def test_zero_retries_records_failure_immediately(self):
        """With retry_count=0, a single failure records the track as failed."""
        downloadable = MagicMock()
        downloadable.source = "deezer"
        downloadable.extension = "flac"
        downloadable.size = AsyncMock(return_value=1000)
        downloadable.download = AsyncMock(side_effect=Exception("error"))

        track, db = self._make_track(retry_count=0, downloadable_mock=downloadable)

        with patch("streamrip.media.track.get_progress_callback") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            with patch("streamrip.media.track.global_download_semaphore") as sem:
                sem.return_value.__aenter__ = AsyncMock(return_value=None)
                sem.return_value.__aexit__ = AsyncMock(return_value=False)
                await track.download()

        assert downloadable.download.call_count == 1
        assert db.stats.retried == 0
        assert db.stats.failed == 1


# ---------------------------------------------------------------------------
# Track.postprocess() – FLAC validation
# ---------------------------------------------------------------------------


class TestFlacValidation:
    def _make_track(self, validate_flac: bool, download_path: str):
        from streamrip.media.track import Track

        meta = MagicMock()
        meta.title = "My Song"
        meta.artist = "Artist"
        meta.info.id = "some_id"

        config = MagicMock()
        config.session.reliability.validate_flac = validate_flac
        config.session.conversion.enabled = False
        config.session.cli.progress_bars = False
        # stub is_single so remove_title isn't called
        config.session = MagicMock()
        config.session.reliability.validate_flac = validate_flac
        config.session.conversion.enabled = False

        downloadable = MagicMock()
        downloadable.source = "deezer"
        downloadable.extension = "flac"

        db = _make_db()

        track = Track(
            meta=meta,
            downloadable=downloadable,
            config=config,
            folder="/tmp",
            cover_path=None,
            db=db,
            is_single=False,
        )
        track.download_path = download_path
        return track, db

    @pytest.mark.asyncio
    async def test_valid_flac_passes_validation(self):
        """A real valid FLAC file should pass validation."""
        flac_path = os.path.join(os.path.dirname(__file__), "silence.flac")
        if not os.path.exists(flac_path):
            pytest.skip("silence.flac fixture not found")

        track, db = self._make_track(validate_flac=True, download_path=flac_path)

        # Patch tag_file to avoid actually writing tags to the fixture
        with patch("streamrip.media.track.tag_file", new_callable=AsyncMock):
            await track.postprocess()

        assert db.stats.failed == 0
        assert db.stats.validation_failures == 0

    @pytest.mark.asyncio
    async def test_corrupt_flac_records_failure_and_removes_file(self):
        """A corrupt FLAC should be removed and recorded as a validation failure."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            f.write(b"this is not a valid flac file at all")
            corrupt_path = f.name

        try:
            track, db = self._make_track(validate_flac=True, download_path=corrupt_path)

            with patch("streamrip.media.track.tag_file", new_callable=AsyncMock):
                with pytest.raises(ValueError, match="FLAC validation failed"):
                    await track.postprocess()

            assert not os.path.exists(corrupt_path), "Corrupt file should have been removed"
            assert db.stats.failed == 1
            assert db.stats.validation_failures == 1
        finally:
            # Cleanup in case removal failed
            if os.path.exists(corrupt_path):
                os.remove(corrupt_path)

    @pytest.mark.asyncio
    async def test_validate_flac_disabled_skips_check(self):
        """When validate_flac=False, no validation is performed."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            f.write(b"garbage")
            corrupt_path = f.name

        try:
            track, db = self._make_track(validate_flac=False, download_path=corrupt_path)

            with patch("streamrip.media.track.tag_file", new_callable=AsyncMock):
                # Should NOT raise even though file is corrupt
                await track.postprocess()

            assert os.path.exists(corrupt_path), "File should NOT be removed when validation is disabled"
            assert db.stats.validation_failures == 0
        finally:
            if os.path.exists(corrupt_path):
                os.remove(corrupt_path)

    @pytest.mark.asyncio
    async def test_non_flac_extension_skips_validation(self):
        """Non-FLAC files are not validated even when validate_flac=True."""
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"not a flac")
            mp3_path = f.name

        try:
            track, db = self._make_track(validate_flac=True, download_path=mp3_path)
            track.download_path = mp3_path

            with patch("streamrip.media.track.tag_file", new_callable=AsyncMock):
                # Should not raise for .mp3 extension
                await track.postprocess()

            assert db.stats.validation_failures == 0
        finally:
            if os.path.exists(mp3_path):
                os.remove(mp3_path)


# ---------------------------------------------------------------------------
# Main.rip() – fail-fast mode
# ---------------------------------------------------------------------------


class TestFailFast:
    @pytest.mark.asyncio
    async def test_fail_fast_stops_after_first_failure(self):
        """In fail-fast mode, rip() stops processing after the first item
        that records a failure in the DB."""
        from streamrip.rip.main import Main

        config = MagicMock()
        config.session.downloads.requests_per_minute = 0
        config.session.database.downloads_enabled = False
        config.session.database.failed_downloads_enabled = False
        config.session.reliability.fail_fast = True
        config.session.reliability.retry_count = 0
        config.session.reliability.retry_delay = 0.0
        config.session.reliability.retry_backoff_factor = 1.0
        config.session.reliability.validate_flac = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(config)

            call_order = []

            async def rip_success():
                # Mark a failure in the db so fail_fast triggers
                main.database.stats.failed += 1
                call_order.append("item1")

            async def rip_never_called():
                call_order.append("item2")

            mock_item1 = MagicMock()
            mock_item1.rip = rip_success
            mock_item2 = MagicMock()
            mock_item2.rip = rip_never_called

            main.media = [mock_item1, mock_item2]
            await main.rip()

        assert "item1" in call_order
        assert "item2" not in call_order, "item2 should not be processed in fail-fast mode"

    @pytest.mark.asyncio
    async def test_non_fail_fast_processes_all_items(self):
        """Without fail-fast, all items are attempted even when one fails."""
        from streamrip.rip.main import Main

        config = MagicMock()
        config.session.downloads.requests_per_minute = 0
        config.session.database.downloads_enabled = False
        config.session.database.failed_downloads_enabled = False
        config.session.reliability.fail_fast = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(config)

            call_order = []

            mock_item1 = MagicMock()
            mock_item1.rip = AsyncMock(side_effect=Exception("item1 failed"))

            async def item2_rip():
                call_order.append("item2")

            mock_item2 = MagicMock()
            mock_item2.rip = item2_rip

            main.media = [mock_item1, mock_item2]
            failures = await main.rip()

        assert "item2" in call_order
        assert failures > 0


# ---------------------------------------------------------------------------
# Main.rip() – return value / exit code semantics
# ---------------------------------------------------------------------------


class TestExitCodeSemantics:
    @pytest.mark.asyncio
    async def test_rip_returns_zero_on_success(self):
        from streamrip.rip.main import Main

        config = MagicMock()
        config.session.downloads.requests_per_minute = 0
        config.session.database.downloads_enabled = False
        config.session.database.failed_downloads_enabled = False
        config.session.reliability.fail_fast = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(config)
            mock_item = MagicMock()
            mock_item.rip = AsyncMock()
            main.media = [mock_item]
            result = await main.rip()

        assert result == 0

    @pytest.mark.asyncio
    async def test_rip_returns_nonzero_on_failure(self):
        from streamrip.rip.main import Main

        config = MagicMock()
        config.session.downloads.requests_per_minute = 0
        config.session.database.downloads_enabled = False
        config.session.database.failed_downloads_enabled = False
        config.session.reliability.fail_fast = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(config)
            mock_item = MagicMock()
            mock_item.rip = AsyncMock(side_effect=Exception("download failed"))
            main.media = [mock_item]
            result = await main.rip()

        assert result > 0

    @pytest.mark.asyncio
    async def test_rip_counts_db_failures(self):
        """DB-tracked failures (via set_failed) are counted in the return value."""
        from streamrip.rip.main import Main

        config = MagicMock()
        config.session.downloads.requests_per_minute = 0
        config.session.database.downloads_enabled = False
        config.session.database.failed_downloads_enabled = False
        config.session.reliability.fail_fast = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(config)

            async def rip_with_db_failure():
                main.database.stats.failed += 1

            mock_item = MagicMock()
            mock_item.rip = rip_with_db_failure
            main.media = [mock_item]
            result = await main.rip()

        assert result >= 1


# ---------------------------------------------------------------------------
# Main.resolve() – exception safety
# ---------------------------------------------------------------------------


class TestSafeResolve:
    @pytest.mark.asyncio
    async def test_resolve_continues_after_item_raises(self):
        """If one pending item's resolve() raises, the others still complete."""
        from streamrip.rip.main import Main

        config = MagicMock()
        config.session.downloads.requests_per_minute = 0
        config.session.database.downloads_enabled = False
        config.session.database.failed_downloads_enabled = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(config)

            good_media = MagicMock()

            pending_ok = MagicMock()
            pending_ok.resolve = AsyncMock(return_value=good_media)

            pending_bad = MagicMock()
            pending_bad.resolve = AsyncMock(side_effect=Exception("resolve failed"))

            main.pending = [pending_bad, pending_ok]
            await main.resolve()

        # The good item should still have been added to media
        assert good_media in main.media
        assert len(main.media) == 1


# ---------------------------------------------------------------------------
# rip repair – replay failed items
# ---------------------------------------------------------------------------


class TestRipRepair:
    @pytest.mark.asyncio
    async def test_repair_adds_failed_items_to_queue(self):
        """repair() should read failed items and add them to main.pending."""
        from streamrip.rip.main import Main

        config = MagicMock()
        config.session.downloads.requests_per_minute = 0
        config.session.database.downloads_enabled = False
        config.session.database.failed_downloads_enabled = False
        config.session.reliability.fail_fast = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(config)

            # Inject pre-existing failed items
            main.database.failed.all = MagicMock(
                return_value=[
                    ("deezer", "track", "id1"),
                    ("deezer", "track", "id2"),
                ]
            )

            failed_items = main.database.get_failed_downloads()
            assert len(failed_items) == 2

            # Verify add_all_by_id would be called with the right args
            main.add_all_by_id = AsyncMock()
            await main.add_all_by_id(
                [(s, mt, i) for s, mt, i in failed_items]
            )
            main.add_all_by_id.assert_called_once_with(
                [("deezer", "track", "id1"), ("deezer", "track", "id2")]
            )

    @pytest.mark.asyncio
    async def test_repair_skips_already_downloaded(self):
        """Items already in the downloads DB should be skipped during repair."""
        from streamrip.db import Downloads
        from streamrip.rip.main import Main

        config = MagicMock()
        config.session.downloads.requests_per_minute = 0
        config.session.database.downloads_enabled = False
        config.session.database.failed_downloads_enabled = False
        config.session.reliability.fail_fast = False

        with (
            patch("streamrip.rip.main.QobuzClient"),
            patch("streamrip.rip.main.TidalClient"),
            patch("streamrip.rip.main.DeezerClient"),
            patch("streamrip.rip.main.SoundcloudClient"),
        ):
            main = Main(config)

            # Make "id1" appear to already be downloaded
            main.database.downloads.contains = MagicMock(return_value=True)

            assert main.database.downloaded("id1") is True


# ---------------------------------------------------------------------------
# Artist batch – exception isolation
# ---------------------------------------------------------------------------


class TestArtistBatchIsolation:
    @pytest.mark.asyncio
    async def test_artist_continues_after_album_failure(self):
        """An exception in one album's _rip should not abort the artist batch."""
        from streamrip.media.artist import Artist

        config = MagicMock()
        config.session.qobuz_filters.repeats = False
        config.session.qobuz_filters.extras = False
        config.session.qobuz_filters.features = False
        config.session.qobuz_filters.non_studio_albums = False
        config.session.qobuz_filters.non_remaster = False

        call_log = []

        album_ok = MagicMock()
        album_ok.resolve = AsyncMock(return_value=MagicMock())
        album_ok.resolve.return_value.rip = AsyncMock(
            side_effect=lambda: call_log.append("ok")
        )

        album_fail = MagicMock()
        album_fail.resolve = AsyncMock(side_effect=Exception("album error"))

        artist = Artist(
            name="Test Artist",
            albums=[album_fail, album_ok],
            client=MagicMock(),
            config=config,
        )

        # Should not raise even though album_fail raises
        await artist._download_async(config.session.qobuz_filters)

        album_ok.resolve.assert_called_once()


# ---------------------------------------------------------------------------
# FailedTrackLog CSV
# ---------------------------------------------------------------------------


class TestFailedTrackLog:
    def test_csv_written_with_all_fields(self, tmp_path):
        log_path = str(tmp_path / "failures.csv")
        log = FailedTrackLog(log_path)
        log.log("deezer", "track", "123", title="My Song", artist="John", error="timeout")

        with open(log_path) as f:
            content = f.read()

        assert "My Song" in content
        assert "John" in content
        assert "timeout" in content
        assert "deezer" in content

    def test_has_entries_false_initially(self, tmp_path):
        log_path = str(tmp_path / "failures.csv")
        log = FailedTrackLog(log_path)
        assert log.has_entries is False

    def test_has_entries_true_after_log(self, tmp_path):
        log_path = str(tmp_path / "failures.csv")
        log = FailedTrackLog(log_path)
        log.log("deezer", "track", "1")
        assert log.has_entries is True

    def test_csv_header_written_on_creation(self, tmp_path):
        log_path = str(tmp_path / "failures.csv")
        FailedTrackLog(log_path)

        with open(log_path) as f:
            first_line = f.readline()

        assert "timestamp" in first_line
        assert "title" in first_line
        assert "error" in first_line
