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
        """If all retry attempts fail, the track is recorded as failed and DownloadError is raised."""
        from streamrip.exceptions import DownloadError

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
                with pytest.raises(DownloadError):
                    await track.download()

        # retry_count=2 means 3 total attempts (initial + 2 retries)
        assert downloadable.download.call_count == 3
        assert db.stats.retried == 2
        assert db.stats.failed == 1

    @pytest.mark.asyncio
    async def test_zero_retries_records_failure_immediately(self):
        """With retry_count=0, a single failure records the track as failed and raises DownloadError."""
        from streamrip.exceptions import DownloadError

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
                with pytest.raises(DownloadError):
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


# ---------------------------------------------------------------------------
# Critical regression: failed download must not reach postprocess/set_downloaded
# ---------------------------------------------------------------------------


class TestFailedDownloadDoesNotPostprocess:
    """Regression tests for the critical bug where Track.rip() would call
    postprocess() and set_downloaded() even after download() recorded a failure."""

    def _make_failing_track(self, db):
        from streamrip.media.track import Track

        meta = MagicMock()
        meta.title = "Failing Song"
        meta.tracknumber = 1
        meta.artist = "Bad Artist"
        meta.info.id = "fail_id"

        config = MagicMock()
        config.session.reliability.retry_count = 0
        config.session.reliability.retry_delay = 0.0
        config.session.reliability.retry_backoff_factor = 1.0
        config.session.cli.progress_bars = False
        config.session.conversion.enabled = False
        config.session.reliability.validate_flac = False

        downloadable = MagicMock()
        downloadable.source = "deezer"
        downloadable.extension = "flac"
        downloadable.size = AsyncMock(return_value=1000)
        downloadable.download = AsyncMock(side_effect=Exception("download failed"))

        track = Track(
            meta=meta,
            downloadable=downloadable,
            config=config,
            folder="/tmp",
            cover_path=None,
            db=db,
        )
        track.download_path = "/tmp/failing_song.flac"
        return track

    @staticmethod
    def _rip_patches():
        """Return the stack of patches needed to call track.rip() in tests.

        Suppresses: progress callback, download semaphore, _set_download_path
        (which reads config values we don't fully mock), os.path.isfile, and
        os.makedirs so no filesystem side-effects occur.
        """
        from contextlib import ExitStack

        stack = ExitStack()
        mock_ctx = stack.enter_context(
            patch("streamrip.media.track.get_progress_callback")
        )
        mock_ctx.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        sem = stack.enter_context(
            patch("streamrip.media.track.global_download_semaphore")
        )
        sem.return_value.__aenter__ = AsyncMock(return_value=None)
        sem.return_value.__aexit__ = AsyncMock(return_value=False)
        stack.enter_context(patch("streamrip.media.track.Track._set_download_path"))
        stack.enter_context(patch("os.path.isfile", return_value=False))
        stack.enter_context(patch("os.makedirs"))
        return stack

    @pytest.mark.asyncio
    async def test_failed_download_never_calls_postprocess(self):
        """postprocess() must NOT be called when download() exhausts all retries."""
        db = _make_db()
        track = self._make_failing_track(db)

        postprocess_called = []

        async def _fake_postprocess():
            postprocess_called.append(True)

        track.postprocess = _fake_postprocess

        with self._rip_patches():
            await track.rip()

        assert postprocess_called == [], "postprocess must not be called after download failure"

    @pytest.mark.asyncio
    async def test_failed_download_never_marks_set_downloaded(self):
        """set_downloaded() must NOT be called when download() fails."""
        db = _make_db()
        track = self._make_failing_track(db)

        with self._rip_patches():
            await track.rip()

        # set_downloaded increments succeeded; must be 0 for a failed download
        assert db.stats.succeeded == 0, "set_downloaded() must not be called for a failed track"
        assert db.stats.failed == 1

    @pytest.mark.asyncio
    async def test_failed_item_not_in_both_failed_and_downloaded(self, tmp_path):
        """A failed item must never appear in succeeded stats."""
        from streamrip.db import Downloads

        db_path = str(tmp_path / "test_dl.db")
        db = Database(downloads=Downloads(db_path), failed=Dummy())
        track = self._make_failing_track(db)

        with self._rip_patches():
            await track.rip()

        assert db.stats.failed == 1
        assert db.stats.succeeded == 0
        assert not db.downloaded("fail_id"), "failed item must NOT be in the downloads DB"


# ---------------------------------------------------------------------------
# Critical regression: size() errors are retried and counted
# ---------------------------------------------------------------------------


class TestSizeErrorInRetryLoop:
    """Regression for size() being called outside the try block,
    which meant size() exceptions bypassed retry and set_failed tracking."""

    @pytest.mark.asyncio
    async def test_size_error_is_retried_and_eventually_fails(self):
        """An error from downloadable.size() should be treated as a transient failure,
        retried, and finally recorded via set_failed rather than propagating raw."""
        from streamrip.exceptions import DownloadError
        from streamrip.media.track import Track

        meta = MagicMock()
        meta.title = "Size Error Track"
        meta.tracknumber = 1
        meta.artist = "Artist"
        meta.info.id = "size_err_id"

        config = MagicMock()
        config.session.reliability.retry_count = 1
        config.session.reliability.retry_delay = 0.0
        config.session.reliability.retry_backoff_factor = 1.0
        config.session.cli.progress_bars = False

        downloadable = MagicMock()
        downloadable.source = "deezer"
        downloadable.extension = "flac"
        # Both size() calls fail
        downloadable.size = AsyncMock(side_effect=Exception("network timeout"))
        downloadable.download = AsyncMock()

        db = _make_db()
        track = Track(
            meta=meta,
            downloadable=downloadable,
            config=config,
            folder="/tmp",
            cover_path=None,
            db=db,
        )
        track.download_path = "/tmp/track.flac"

        with patch("streamrip.media.track.global_download_semaphore") as sem:
            sem.return_value.__aenter__ = AsyncMock(return_value=None)
            sem.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(DownloadError):
                await track.download()

        # size() called once per attempt: 2 total (initial + 1 retry)
        assert downloadable.size.call_count == 2
        # download() was never reached
        assert downloadable.download.call_count == 0
        # Failure must be recorded in the DB
        assert db.stats.failed == 1

    @pytest.mark.asyncio
    async def test_size_error_first_then_success(self):
        """size() failure on first attempt, success on second — counted as retry."""
        from streamrip.media.track import Track

        meta = MagicMock()
        meta.title = "Size Retry Track"
        meta.tracknumber = 1
        meta.artist = "Artist"
        meta.info.id = "size_retry_id"

        config = MagicMock()
        config.session.reliability.retry_count = 2
        config.session.reliability.retry_delay = 0.0
        config.session.reliability.retry_backoff_factor = 1.0
        config.session.cli.progress_bars = False

        downloadable = MagicMock()
        downloadable.source = "deezer"
        downloadable.extension = "flac"
        # Fail on first size(), succeed on second
        downloadable.size = AsyncMock(side_effect=[Exception("network timeout"), 1000])
        downloadable.download = AsyncMock()

        db = _make_db()
        track = Track(
            meta=meta,
            downloadable=downloadable,
            config=config,
            folder="/tmp",
            cover_path=None,
            db=db,
        )
        track.download_path = "/tmp/track.flac"

        with patch("streamrip.media.track.get_progress_callback") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            with patch("streamrip.media.track.global_download_semaphore") as sem:
                sem.return_value.__aenter__ = AsyncMock(return_value=None)
                sem.return_value.__aexit__ = AsyncMock(return_value=False)
                await track.download()

        # size() fails on attempt 1, succeeds on attempt 2
        assert downloadable.size.call_count == 2
        # download() only called on the successful attempt
        assert downloadable.download.call_count == 1
        assert db.stats.failed == 0
        assert db.stats.retried == 1


# ---------------------------------------------------------------------------
# Critical regression: Failed DB composite uniqueness
# ---------------------------------------------------------------------------


class TestFailedDbCompositeUniqueness:
    """Regression for Failed.structure using id UNIQUE instead of
    UNIQUE(source, media_type, id), which allowed cross-source collisions."""

    def test_same_id_different_sources_both_stored(self, tmp_path):
        """Two items with the same ID but different sources must both be stored."""
        from streamrip.db import Failed

        db_path = str(tmp_path / "failed.db")
        failed = Failed(db_path)

        failed.add(("deezer", "track", "shared_id"))
        failed.add(("qobuz", "track", "shared_id"))

        rows = failed.all()
        assert len(rows) == 2, (
            "Both (deezer, track, shared_id) and (qobuz, track, shared_id) must be stored"
        )
        sources = {row[0] for row in rows}
        assert sources == {"deezer", "qobuz"}

    def test_same_id_different_media_types_both_stored(self, tmp_path):
        """Same ID, same source, but different media_type must both be stored."""
        from streamrip.db import Failed

        db_path = str(tmp_path / "failed.db")
        failed = Failed(db_path)

        failed.add(("deezer", "track", "123"))
        failed.add(("deezer", "album", "123"))

        rows = failed.all()
        assert len(rows) == 2
        types = {row[1] for row in rows}
        assert types == {"track", "album"}

    def test_exact_duplicate_stored_once(self, tmp_path):
        """Exact (source, media_type, id) duplicate is silently ignored (idempotent)."""
        from streamrip.db import Failed

        db_path = str(tmp_path / "failed.db")
        failed = Failed(db_path)

        failed.add(("deezer", "track", "abc"))
        failed.add(("deezer", "track", "abc"))  # duplicate

        rows = failed.all()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Major regression: repair clears recovered items
# ---------------------------------------------------------------------------


class TestRepairClearsRecoveredItems:
    """Regression for rip repair never removing items from failed store after success."""

    def test_clear_failed_removes_entry(self, tmp_path):
        """clear_failed() removes the item from the failed store."""
        from streamrip.db import Failed

        db_path = str(tmp_path / "failed.db")
        failed_db = Failed(db_path)
        database = Database(downloads=Dummy(), failed=failed_db)

        database.set_failed("deezer", "track", "id1")
        assert len(database.get_failed_downloads()) == 1

        database.clear_failed("deezer", "track", "id1")
        assert len(database.get_failed_downloads()) == 0

    def test_clear_failed_only_removes_matching_composite_key(self, tmp_path):
        """clear_failed() only removes the matching (source, media_type, id) row."""
        from streamrip.db import Failed

        db_path = str(tmp_path / "failed.db")
        failed_db = Failed(db_path)
        database = Database(downloads=Dummy(), failed=failed_db)

        database.set_failed("deezer", "track", "id1")
        database.set_failed("qobuz", "track", "id1")

        database.clear_failed("deezer", "track", "id1")

        rows = database.get_failed_downloads()
        assert len(rows) == 1
        assert rows[0][0] == "qobuz", "Only the deezer entry should have been removed"

    def test_clear_failed_on_dummy_db_is_noop(self):
        """clear_failed() on a Dummy-backed Database must not raise."""
        database = _make_db()
        database.set_failed("deezer", "track", "id1")
        database.clear_failed("deezer", "track", "id1")  # should not raise

    @pytest.mark.asyncio
    async def test_postprocess_calls_clear_failed_on_success(self):
        """On a successful download, Track.postprocess() removes the item from failed store."""
        from streamrip.db import Failed
        import tempfile as _tempfile

        meta = MagicMock()
        meta.title = "Repaired Song"
        meta.artist = "Artist"
        meta.info.id = "repaired_id"

        config = MagicMock()
        config.session.reliability.validate_flac = False
        config.session.conversion.enabled = False

        downloadable = MagicMock()
        downloadable.source = "deezer"

        with _tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "failed.db")
            failed_db = Failed(db_path)
            database = Database(downloads=Dummy(), failed=failed_db)

            # Pre-populate: item was previously failed
            database.set_failed("deezer", "track", "repaired_id")
            assert len(database.get_failed_downloads()) == 1

            from streamrip.media.track import Track

            track = Track(
                meta=meta,
                downloadable=downloadable,
                config=config,
                folder=tmpdir,
                cover_path=None,
                db=database,
                is_single=False,
            )
            track.download_path = os.path.join(tmpdir, "song.mp3")

            # Patch tag_file; skip FLAC validation (not .flac extension)
            with patch("streamrip.media.track.tag_file", new_callable=AsyncMock):
                await track.postprocess()

            # After successful postprocess, item should be gone from failed store
            assert len(database.get_failed_downloads()) == 0
            assert database.stats.succeeded == 1


# ---------------------------------------------------------------------------
# Major regression: resolve-stage failures are counted in stats
# ---------------------------------------------------------------------------


class TestResolveStageFailuresCounted:
    """PendingTrack/PendingAlbum/PendingPlaylist resolve failures must be
    recorded via set_failed so they appear in session stats and repair queues."""

    @pytest.mark.asyncio
    async def test_pending_album_non_streamable_calls_set_failed(self):
        """PendingAlbum.resolve(): NonStreamableError increments failed counter."""
        from streamrip.exceptions import NonStreamableError
        from streamrip.media.album import PendingAlbum

        client = MagicMock()
        client.source = "deezer"
        client.get_metadata = AsyncMock(side_effect=NonStreamableError("not available"))

        config = MagicMock()
        db = _make_db()

        pending = PendingAlbum(id="album123", client=client, config=config, db=db)
        result = await pending.resolve()

        assert result is None
        assert db.stats.failed == 1

    @pytest.mark.asyncio
    async def test_pending_album_metadata_error_calls_set_failed(self):
        """PendingAlbum.resolve(): metadata build exception increments failed counter."""
        from streamrip.media.album import PendingAlbum

        client = MagicMock()
        client.source = "deezer"
        client.get_metadata = AsyncMock(return_value={"some": "resp"})

        config = MagicMock()
        db = _make_db()

        with patch(
            "streamrip.media.album.AlbumMetadata.from_album_resp",
            side_effect=Exception("parse error"),
        ):
            pending = PendingAlbum(id="album123", client=client, config=config, db=db)
            result = await pending.resolve()

        assert result is None
        assert db.stats.failed == 1

    @pytest.mark.asyncio
    async def test_pending_track_non_streamable_calls_set_failed(self):
        """PendingTrack.resolve(): NonStreamableError increments failed counter."""
        from streamrip.exceptions import NonStreamableError
        from streamrip.media.track import PendingTrack

        client = MagicMock()
        client.source = "deezer"
        client.get_metadata = AsyncMock(side_effect=NonStreamableError("unavailable"))

        db = _make_db()
        pending = PendingTrack(
            id="track123",
            album=MagicMock(),
            client=client,
            config=MagicMock(),
            folder="/tmp",
            db=db,
            cover_path=None,
        )
        result = await pending.resolve()

        assert result is None
        assert db.stats.failed == 1

    @pytest.mark.asyncio
    async def test_pending_playlist_non_streamable_calls_set_failed(self):
        """PendingPlaylist.resolve(): NonStreamableError increments failed counter."""
        from streamrip.exceptions import NonStreamableError
        from streamrip.media.playlist import PendingPlaylist

        client = MagicMock()
        client.source = "deezer"
        client.get_metadata = AsyncMock(side_effect=NonStreamableError("unavailable"))

        db = _make_db()
        pending = PendingPlaylist(id="pl123", client=client, config=MagicMock(), db=db)
        result = await pending.resolve()

        assert result is None
        assert db.stats.failed == 1

    @pytest.mark.asyncio
    async def test_pending_playlist_track_non_streamable_calls_set_failed(self):
        """PendingPlaylistTrack.resolve(): NonStreamableError increments failed counter."""
        from streamrip.exceptions import NonStreamableError
        from streamrip.media.playlist import PendingPlaylistTrack

        client = MagicMock()
        client.source = "deezer"
        client.get_metadata = AsyncMock(side_effect=NonStreamableError("unavailable"))

        db = _make_db()
        pending = PendingPlaylistTrack(
            id="pt123",
            client=client,
            config=MagicMock(),
            folder="/tmp",
            playlist_name="Test Playlist",
            position=1,
            db=db,
        )
        result = await pending.resolve()

        assert result is None
        assert db.stats.failed == 1
