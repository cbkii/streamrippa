"""Tests for source-aware downloaded checks in streamrip.db."""

from __future__ import annotations

from streamrip.db import Database


class _MemoryDb:
    """Minimal in-memory DatabaseInterface for testing."""

    def __init__(self):
        self._data: set[str] = set()

    def add(self, item: tuple):
        self._data.add(item[0])

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
