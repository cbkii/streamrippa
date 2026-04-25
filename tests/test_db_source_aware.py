"""Tests for source-aware downloaded checks in streamrip.db."""

from __future__ import annotations

from streamrip.db import Database


class _MemoryDb:
    """Minimal in-memory DatabaseInterface for testing."""

    def __init__(self):
        self._data: set[str] = set()

    def add(self, item: tuple):
        before = len(self._data)
        self._data.add(item[0])
        return len(self._data) > before

    def contains(self, **kwargs) -> bool:
        return kwargs.get("id", "") in self._data

    def all(self):
        return list(self._data)

    def remove(self, **kwargs):
        key = kwargs.get("id", "")
        self._data.discard(key)


def _make_db():
    return Database(downloads=_MemoryDb(), failed=_MemoryDb())


# ---------------------------------------------------------------------------
# Source-aware downloaded checks
# ---------------------------------------------------------------------------


def test_set_downloaded_source_aware():
    db = _make_db()
    db.set_downloaded("123", source="deezer")
    # Source-aware key should exist
    assert db.downloads.contains(id="deezer:123")
    # Plain key should not exist
    assert not db.downloads.contains(id="123")


def test_downloaded_returns_true_source_aware():
    db = _make_db()
    db.set_downloaded("123", source="deezer")
    assert db.downloaded("123", source="deezer")


def test_downloaded_no_false_cross_source_collision():
    """Deezer ID 123 and Qobuz ID 123 must NOT collide."""
    db = _make_db()
    db.set_downloaded("123", source="deezer")
    # Qobuz should not see it as downloaded
    assert not db.downloaded("123", source="qobuz")


def test_downloaded_legacy_plain_id():
    """Old plain-id records (no source prefix) are still recognised."""
    db = _make_db()
    # Simulate a legacy record written without source
    db.downloads.add(("123",))
    # Legacy plain-ID check still works
    assert db.downloaded("123")
    # Source-aware check also finds legacy record via fallback
    assert db.downloaded("123", source="deezer")


def test_set_downloaded_without_source_uses_plain_id():
    db = _make_db()
    db.set_downloaded("456")
    assert db.downloads.contains(id="456")
    assert not db.downloads.contains(id="deezer:456")


def test_downloaded_returns_false_for_unknown_id():
    db = _make_db()
    assert not db.downloaded("999")
    assert not db.downloaded("999", source="qobuz")


def test_stats_increment_on_set_downloaded():
    db = _make_db()
    db.set_downloaded("111", source="qobuz")
    assert db.stats.succeeded == 1


def test_set_downloaded_duplicate_counts_as_skipped():
    db = _make_db()
    db.set_downloaded("111", source="qobuz")
    db.set_downloaded("111", source="qobuz")
    assert db.stats.succeeded == 1
    assert db.stats.skipped == 1


def test_set_downloaded_count_stats_false_does_not_increment():
    db = _make_db()
    db.set_downloaded("111", source="qobuz", count_stats=False)
    assert db.stats.succeeded == 0
    assert db.stats.skipped == 0
    db.set_downloaded("111", source="qobuz", count_stats=False)
    assert db.stats.succeeded == 0
    assert db.stats.skipped == 0


# ---------------------------------------------------------------------------
# Failed store and clear_failed
# ---------------------------------------------------------------------------


class _MemoryFailedDb:
    """In-memory DatabaseInterface that supports composite (source, media_type, id) keys."""

    def __init__(self):
        self._data: set[tuple[str, str, str]] = set()

    def add(self, item: tuple):
        before = len(self._data)
        self._data.add(item)
        return len(self._data) > before

    def contains(self, **kwargs) -> bool:
        key = (
            kwargs.get("source", ""),
            kwargs.get("media_type", ""),
            kwargs.get("id", ""),
        )
        return key in self._data

    def all(self):
        return list(self._data)

    def remove(self, **kwargs):
        key = (
            kwargs.get("source", ""),
            kwargs.get("media_type", ""),
            kwargs.get("id", ""),
        )
        self._data.discard(key)


def _make_db_with_failed():
    return Database(downloads=_MemoryDb(), failed=_MemoryFailedDb())


def test_set_failed_records_composite_key():
    db = _make_db_with_failed()
    db.set_failed("deezer", "track", "100", title="Song A", error="timeout")
    assert db.failed.contains(source="deezer", media_type="track", id="100")
    assert db.stats.failed == 1


def test_set_failed_validation_increments_validation_counter():
    db = _make_db_with_failed()
    db.set_failed("deezer", "track", "100", is_validation_failure=True)
    assert db.stats.validation_failures == 1
    assert db.stats.failed == 1


def test_set_failed_duplicate_does_not_double_count():
    db = _make_db_with_failed()
    db.set_failed("deezer", "track", "100")
    db.set_failed("deezer", "track", "100")
    assert db.stats.failed == 1


def test_clear_failed_removes_item():
    db = _make_db_with_failed()
    db.set_failed("qobuz", "track", "200")
    assert db.failed.contains(source="qobuz", media_type="track", id="200")
    db.clear_failed("qobuz", "track", "200")
    assert not db.failed.contains(source="qobuz", media_type="track", id="200")


def test_clear_failed_is_idempotent():
    """Calling clear_failed twice must not raise."""
    db = _make_db_with_failed()
    db.set_failed("deezer", "track", "300")
    db.clear_failed("deezer", "track", "300")
    db.clear_failed("deezer", "track", "300")  # second call is no-op
    assert not db.failed.contains(source="deezer", media_type="track", id="300")


def test_clear_failed_does_not_affect_other_sources():
    """Clearing a failed record for one source must not affect another."""
    db = _make_db_with_failed()
    db.set_failed("deezer", "track", "400")
    db.set_failed("qobuz", "track", "400")
    db.clear_failed("deezer", "track", "400")
    assert not db.failed.contains(source="deezer", media_type="track", id="400")
    assert db.failed.contains(source="qobuz", media_type="track", id="400")


def test_get_failed_downloads():
    db = _make_db_with_failed()
    db.set_failed("deezer", "track", "500")
    db.set_failed("qobuz", "album", "600")
    failed = db.get_failed_downloads()
    assert len(failed) == 2
