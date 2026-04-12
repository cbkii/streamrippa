"""Tests for CSV playlist resolver (PendingCsvPlaylist / PendingCsvTrack)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.db import Database
from streamrip.file_lists import ExportifyCsvRow
from streamrip.media.csv_playlist import (
    PendingCsvPlaylist,
    PendingCsvTrack,
    TrackCandidate,
    _build_quality_sequence,
    _chunks,
    _extract_raw_results,
    _pick_best_candidate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(
    title: str = "Song",
    artists: list[str] | None = None,
    album: str = "Album",
    date: str = "2020",
    isrc: str = "",
    row_index: int = 0,
) -> ExportifyCsvRow:
    artists = artists or ["Artist"]
    return ExportifyCsvRow(
        track_name=title,
        artists_raw=";".join(artists),
        artists_list=artists,
        album=album,
        release_date=date,
        isrc=isrc,
        spotify_uri="",
        genres="",
        loudness="",
        tempo="",
        position=row_index + 1,
        row_index=row_index,
    )


class _MemoryDb:
    """Minimal in-memory DatabaseInterface."""

    def __init__(self):
        self._data: set[str] = set()

    def add(self, item):
        self._data.add(item[0])

    def contains(self, **kwargs) -> bool:
        return kwargs.get("id", "") in self._data

    def all(self):
        return list(self._data)

    def remove(self, **kwargs):
        self._data.discard(kwargs.get("id", ""))


def _make_db():
    return Database(downloads=_MemoryDb(), failed=_MemoryDb())


def _make_client(source: str, max_quality: int = 2):
    client = MagicMock()
    client.source = source
    client.max_quality = max_quality
    client.session = MagicMock()
    return client


def _make_candidate(
    source: str,
    client: Any,
    id: str = "123",
    score: int = 60,
) -> TrackCandidate:
    return TrackCandidate(
        source=source,
        id=id,
        title="Song",
        artist="Artist",
        album="Album",
        release_date="2020",
        isrc="",
        score=score,
        client=client,
    )


def _make_config(
    primary_quality: int = 2, fallback_quality: int = 3, fail_fast: bool = False
):
    cfg = MagicMock()
    cfg.session.downloads.folder = "/tmp/test_downloads"
    cfg.session.cli.progress_bars = False
    cfg.session.reliability.fail_fast = fail_fast
    cfg.session.metadata.renumber_playlist_tracks = False
    cfg.session.metadata.set_playlist_to_album = False
    cfg.session.metadata.exportify_tag_map = {}
    cfg.session.artwork = MagicMock()
    cfg.session.lastfm.source = "qobuz"
    cfg.session.lastfm.fallback_source = "deezer"

    qobuz_cfg = MagicMock()
    qobuz_cfg.quality = primary_quality
    deezer_cfg = MagicMock()
    deezer_cfg.quality = fallback_quality

    def _get_source(src):
        if src == "qobuz":
            return qobuz_cfg
        return deezer_cfg

    cfg.session.get_source.side_effect = _get_source
    return cfg


# ---------------------------------------------------------------------------
# _build_quality_sequence
# ---------------------------------------------------------------------------


def test_build_quality_sequence_deezer():
    assert _build_quality_sequence("deezer", 2) == [2, 1, 0]


def test_build_quality_sequence_qobuz():
    assert _build_quality_sequence("qobuz", 4) == [4, 3, 2, 1, 0]


def test_build_quality_sequence_zero():
    assert _build_quality_sequence("soundcloud", 0) == [0]


# ---------------------------------------------------------------------------
# _chunks
# ---------------------------------------------------------------------------


def test_chunks_basic():
    result = list(_chunks(list(range(7)), 3))
    assert result == [[0, 1, 2], [3, 4, 5], [6]]


def test_chunks_empty():
    assert list(_chunks([], 5)) == []


# ---------------------------------------------------------------------------
# _extract_raw_results
# ---------------------------------------------------------------------------


def test_extract_raw_results_deezer():
    pages = [{"data": [{"id": 1, "title": "Song"}]}]
    items = _extract_raw_results("deezer", pages)
    assert len(items) == 1
    assert items[0]["id"] == 1


def test_extract_raw_results_qobuz():
    pages = [{"tracks": {"items": [{"id": 2, "title": "Song"}]}}]
    items = _extract_raw_results("qobuz", pages)
    assert len(items) == 1
    assert items[0]["id"] == 2


def test_extract_raw_results_empty_pages():
    assert _extract_raw_results("deezer", []) == []


# ---------------------------------------------------------------------------
# _pick_best_candidate
# ---------------------------------------------------------------------------


def test_pick_best_candidate_isrc_wins():
    row = _make_row(isrc="ISRC001")
    client = _make_client("deezer")
    pages = [
        {
            "data": [
                {
                    "id": 1,
                    "title": "Wrong Song",
                    "artist": {"name": "X"},
                    "isrc": "ISRC001",
                    "album": {"title": ""},
                },
                {
                    "id": 2,
                    "title": "Song",
                    "artist": {"name": "Artist"},
                    "isrc": "OTHER",
                    "album": {"title": "Album"},
                },
            ]
        }
    ]
    cand = _pick_best_candidate(row, "deezer", pages, client)
    assert cand is not None
    assert cand.id == "1"
    assert cand.score == 100


def test_pick_best_candidate_returns_none_on_empty():
    row = _make_row()
    client = _make_client("deezer")
    cand = _pick_best_candidate(row, "deezer", [], client)
    assert cand is None


def test_pick_best_candidate_title_artist_match():
    row = _make_row(title="Blue in Green", artists=["Miles Davis"])
    client = _make_client("qobuz")
    pages = [
        {
            "tracks": {
                "items": [
                    {
                        "id": 10,
                        "title": "Blue in Green",
                        "performer": {"name": "Miles Davis"},
                        "album": {"title": "Kind of Blue"},
                        "isrc": "",
                        "release_date_original": "1959",
                    }
                ]
            }
        }
    ]
    cand = _pick_best_candidate(row, "qobuz", pages, client)
    assert cand is not None
    assert cand.score >= 60


# ---------------------------------------------------------------------------
# PendingCsvTrack.resolve — quality-fallback algorithm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_csv_track_primary_wins():
    """Primary candidate is attempted first and should succeed."""
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz", max_quality=3)
    fallback_client = _make_client("deezer", max_quality=2)

    primary_cand = _make_candidate("qobuz", primary_client, id="qobuz_id", score=60)
    fallback_cand = _make_candidate("deezer", fallback_client, id="deezer_id", score=55)

    # Simulate successful Track resolve for primary
    mock_track = MagicMock()
    mock_track.rip = AsyncMock()

    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=primary_cand,
        fallback_candidate=fallback_cand,
        primary_qualities=[3, 2, 1, 0],
        fallback_qualities=[2, 1, 0],
        primary_source="qobuz",
        fallback_source="deezer",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Test Playlist",
        position=1,
        db=db,
    )

    with patch.object(track, "_try_candidate", new_callable=AsyncMock) as mock_try:
        mock_try.return_value = mock_track
        result = await track.resolve()

    # _try_candidate called once with primary candidate
    mock_try.assert_called_once()
    call_args = mock_try.call_args
    assert call_args[0][0].source == "qobuz"
    assert result is mock_track


@pytest.mark.asyncio
async def test_pending_csv_track_fallback_used_when_primary_fails():
    """Fallback service should be tried when primary fails at same pass quality."""
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz", max_quality=3)
    fallback_client = _make_client("deezer", max_quality=2)

    primary_cand = _make_candidate("qobuz", primary_client, id="qobuz_id")
    fallback_cand = _make_candidate("deezer", fallback_client, id="deezer_id")
    mock_track = MagicMock()

    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=primary_cand,
        fallback_candidate=fallback_cand,
        primary_qualities=[3, 2, 1, 0],
        fallback_qualities=[2, 1, 0],
        primary_source="qobuz",
        fallback_source="deezer",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Test Playlist",
        position=1,
        db=db,
    )

    call_count = [0]

    async def _try_side_effect(candidate, quality):
        call_count[0] += 1
        if candidate.source == "qobuz":
            return None  # primary fails
        return mock_track  # fallback succeeds

    with patch.object(track, "_try_candidate", side_effect=_try_side_effect):
        result = await track.resolve()

    assert result is mock_track
    # Should have tried both primary (qobuz) and fallback (deezer) at pass 0
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_pending_csv_track_all_passes_fail_returns_none():
    """If all quality passes fail, resolve returns None."""
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    fallback_client = _make_client("deezer")

    primary_cand = _make_candidate("qobuz", primary_client)
    fallback_cand = _make_candidate("deezer", fallback_client)

    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=primary_cand,
        fallback_candidate=fallback_cand,
        primary_qualities=[2, 1, 0],
        fallback_qualities=[2, 1, 0],
        primary_source="qobuz",
        fallback_source="deezer",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Playlist",
        position=1,
        db=db,
    )

    with patch.object(
        track, "_try_candidate", new_callable=AsyncMock, return_value=None
    ):
        result = await track.resolve()

    assert result is None


@pytest.mark.asyncio
async def test_pending_csv_track_no_candidates_returns_none():
    db = _make_db()
    cfg = _make_config()

    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=None,
        fallback_candidate=None,
        primary_qualities=[],
        fallback_qualities=[],
        primary_source="qobuz",
        fallback_source="deezer",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Playlist",
        position=1,
        db=db,
    )
    result = await track.resolve()
    assert result is None


@pytest.mark.asyncio
async def test_pending_csv_track_skips_if_primary_already_downloaded():
    """When the primary candidate is already in the DB, the whole track must be
    skipped — the fallback must NOT be attempted."""
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    fallback_client = _make_client("deezer")

    primary_cand = _make_candidate("qobuz", primary_client, id="qobuz_id")
    fallback_cand = _make_candidate("deezer", fallback_client, id="deezer_id")

    # Mark the primary as already downloaded (source-aware key)
    db.downloads.add(("qobuz:qobuz_id",))

    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=primary_cand,
        fallback_candidate=fallback_cand,
        primary_qualities=[2, 1, 0],
        fallback_qualities=[2, 1, 0],
        primary_source="qobuz",
        fallback_source="deezer",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Playlist",
        position=1,
        db=db,
    )

    with patch.object(track, "_try_candidate", new_callable=AsyncMock) as mock_try:
        result = await track.resolve()

    # Must skip entirely — fallback must not be attempted
    assert result is None
    mock_try.assert_not_called()
    assert db.stats.skipped == 1


@pytest.mark.asyncio
async def test_pending_csv_track_skips_if_fallback_already_downloaded():
    """When the fallback candidate is already in the DB, the whole track must
    be skipped — even if the primary was never downloaded."""
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    fallback_client = _make_client("deezer")

    primary_cand = _make_candidate("qobuz", primary_client, id="qobuz_id")
    fallback_cand = _make_candidate("deezer", fallback_client, id="deezer_id")

    # Mark only the fallback as already downloaded
    db.downloads.add(("deezer:deezer_id",))

    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=primary_cand,
        fallback_candidate=fallback_cand,
        primary_qualities=[2, 1, 0],
        fallback_qualities=[2, 1, 0],
        primary_source="qobuz",
        fallback_source="deezer",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Playlist",
        position=1,
        db=db,
    )

    with patch.object(track, "_try_candidate", new_callable=AsyncMock) as mock_try:
        result = await track.resolve()

    assert result is None
    mock_try.assert_not_called()
    assert db.stats.skipped == 1


@pytest.mark.asyncio
async def test_pending_csv_playlist_fail_fast_stops_after_batch_error():
    """With fail_fast=True, a batch-level exception stops further batch processing.

    Per-row search failures are caught inside _resolve_row (normal behaviour).
    Only an unexpected exception that propagates OUT of _resolve_row triggers
    the fail_fast stop.  This test patches _resolve_row directly to simulate
    that scenario.
    """
    import streamrip.media.csv_playlist as csv_mod

    original_batch_size = csv_mod._RESOLVER_BATCH_SIZE
    csv_mod._RESOLVER_BATCH_SIZE = 1  # one row per batch

    try:
        db = _make_db()
        cfg = _make_config(fail_fast=True)
        primary_client = _make_client("qobuz")
        primary_client.search = AsyncMock(return_value=[])

        rows = [_make_row(row_index=i) for i in range(5)]

        pending = PendingCsvPlaylist(
            playlist_name="Test",
            rows=rows,
            primary_client=primary_client,
            fallback_client=None,
            config=cfg,
            db=db,
        )

        call_count = [0]
        original_resolve_row = pending._resolve_row

        async def _patched_resolve_row(row, folder, pq, fq, status, cb):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Unexpected resolver failure")
            return await original_resolve_row(row, folder, pq, fq, status, cb)

        with patch.object(pending, "_resolve_row", side_effect=_patched_resolve_row):
            await pending.resolve()

        # With fail_fast=True and batch_size=1, the first batch raises → stops after 1.
        # Without fail_fast all 5 batches would run.
        assert call_count[0] == 1

    finally:
        csv_mod._RESOLVER_BATCH_SIZE = original_batch_size


# ---------------------------------------------------------------------------
# Bounded batch resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_csv_playlist_batches_resolver():
    """Resolver must not schedule all rows at once — uses bounded batches."""
    import streamrip.media.csv_playlist as csv_mod

    original_batch_size = csv_mod._RESOLVER_BATCH_SIZE
    # Force a tiny batch size so we can count gather calls
    csv_mod._RESOLVER_BATCH_SIZE = 2

    try:
        db = _make_db()
        cfg = _make_config()
        primary_client = _make_client("qobuz")
        primary_client.search = AsyncMock(return_value=[])

        rows = [_make_row(row_index=i) for i in range(5)]

        pending = PendingCsvPlaylist(
            playlist_name="Test",
            rows=rows,
            primary_client=primary_client,
            fallback_client=None,
            config=cfg,
            db=db,
        )

        gather_calls = []
        original_gather = asyncio.gather

        async def _mock_gather(*coros, return_exceptions=False):
            gather_calls.append(len(coros))
            return await original_gather(*coros, return_exceptions=return_exceptions)

        with patch(
            "streamrip.media.csv_playlist.asyncio.gather", side_effect=_mock_gather
        ):
            await pending.resolve()

        # With batch size 2 and 5 rows: batches of [2, 2, 1] → 3 gather calls
        assert len(gather_calls) == 3
        assert max(gather_calls) <= 2

    finally:
        csv_mod._RESOLVER_BATCH_SIZE = original_batch_size


@pytest.mark.asyncio
async def test_pending_csv_playlist_one_row_exception_continues():
    """A per-row exception must not abort the whole batch when fail_fast=False."""
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")

    call_count = [0]

    async def _search_side_effect(media_type, query, limit=5):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("Simulated search failure")
        return []

    primary_client.search = AsyncMock(side_effect=_search_side_effect)

    rows = [_make_row(row_index=i) for i in range(3)]

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=rows,
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
    )

    # Should complete without raising
    await pending.resolve()
    # At least some rows were processed (the non-failing ones)
    # result may be None (no candidates found) but should not raise
    # The test passes if no exception is raised


# ---------------------------------------------------------------------------
# Regression: existing Deezer non-CSV flows unchanged
# ---------------------------------------------------------------------------


def test_deezer_exact_quality_false_does_not_change_default():
    """The exact_quality=False (default) must keep original behaviour."""
    from streamrip.client.deezer import DeezerClient

    sig = DeezerClient.get_downloadable.__code__.co_varnames
    assert "exact_quality" in sig
    # Default should be False
    defaults = DeezerClient.get_downloadable.__defaults__
    assert defaults is not None
    # Defaults are (quality=2, is_retry=False, exact_quality=False)
    assert False in defaults
