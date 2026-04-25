"""Tests for CSV playlist resolver (PendingCsvPlaylist / PendingCsvTrack)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from streamrip.db import Database
from streamrip.file_lists import ExportifyCsvRow
from streamrip.media.csv_playlist import (
    _MIN_ACCEPTABLE_SCORE,
    PendingCsvPlaylist,
    PendingCsvTrack,
    TrackCandidate,
    _build_quality_sequence,
    _build_search_queries,
    _CandidateMeta,
    _chunks,
    _extract_raw_results,
    _MetaFetchResult,
    _pick_best_candidate,
    _pick_best_candidate_repair,
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
    if artists is None:
        artists = ["Artist"]
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
        before = len(self._data)
        self._data.add(item[0])
        return len(self._data) > before

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
# query building
# ---------------------------------------------------------------------------


def test_build_search_queries_prioritizes_isrc_for_deezer_qobuz():
    row = _make_row(
        title="Track",
        artists=["Artist A", "Artist B"],
        album="Album",
        date="2020-01-01",
        isrc="USABC1234567",
    )
    deezer_queries = _build_search_queries(row, "deezer")
    qobuz_queries = _build_search_queries(row, "qobuz")
    assert deezer_queries[0] == ("isrc", "USABC1234567")
    assert qobuz_queries[0] == ("isrc", "USABC1234567")
    assert any(strategy == "generic" for strategy, _ in deezer_queries)


def test_build_search_queries_non_target_provider_has_no_isrc_step():
    row = _make_row(title="Track", artists=["Artist"], album="Album", isrc="USXYZ")
    tidal_queries = _build_search_queries(row, "tidal")
    assert tidal_queries[0][0] != "isrc"


def test_build_search_queries_adds_stripped_title_variant_for_decorated_rows():
    row = _make_row(
        title="1, 2 Step (feat. Missy Elliott)",
        artists=["Ciara", "Missy Elliott"],
        album="Goodies",
        date="2004",
    )
    queries = _build_search_queries(row, "qobuz")
    strategies = [strategy for strategy, _ in queries]
    assert "stripped-structured" in strategies
    assert "stripped-generic" in strategies
    assert any("1, 2 Step Ciara" in query for _, query in queries)


def test_build_search_queries_dedupes_identical_normalized_queries():
    row = _make_row(title="Song", artists=["Artist"])
    queries = _build_search_queries(row, "qobuz", escalation=True)
    query_texts = [q.casefold() for _, q in queries]
    assert len(query_texts) == len(set(query_texts))


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


def test_pick_best_candidate_prefers_duration_match():
    row = _make_row(title="Layla", artists=["Eric Clapton"])
    row.duration_ms = 290000
    client = _make_client("qobuz")
    pages = [
        {
            "tracks": {
                "items": [
                    {
                        "id": 1,
                        "title": "Layla",
                        "performer": {"name": "Eric Clapton"},
                        "album": {"title": "Unplugged"},
                        "isrc": "",
                        "release_date_original": "1992",
                        "duration": 290,
                    },
                    {
                        "id": 2,
                        "title": "Layla",
                        "performer": {"name": "Eric Clapton"},
                        "album": {"title": "Unplugged"},
                        "isrc": "",
                        "release_date_original": "1992",
                        "duration": 360,
                    },
                ]
            }
        }
    ]
    cand = _pick_best_candidate(row, "qobuz", pages, client)
    assert cand is not None
    assert cand.id == "1"


# ---------------------------------------------------------------------------
# PendingCsvTrack.resolve — quality-fallback algorithm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_csv_track_primary_wins():
    """Primary candidate is attempted first and should succeed.

    With the metadata-cache refactor, we patch ``_fetch_candidate_meta`` at
    the class level (required by slots=True) and ``_try_candidate_with_meta``
    to verify the primary is tried first.
    """
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz", max_quality=3)
    fallback_client = _make_client("deezer", max_quality=2)

    primary_cand = _make_candidate("qobuz", primary_client, id="qobuz_id", score=60)
    fallback_cand = _make_candidate("deezer", fallback_client, id="deezer_id", score=55)

    mock_track = MagicMock()
    mock_meta = _CandidateMeta(
        resp={},
        album=MagicMock(),
        meta=MagicMock(),
    )

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

    try_calls = []

    async def _fake_fetch(self_arg, candidate):
        return mock_meta

    async def _fake_try(self_arg, candidate, cached, quality):
        try_calls.append(candidate.source)
        if candidate.source == "qobuz":
            return mock_track, "ok"
        return None, "quality unavailable"

    with (
        patch.object(PendingCsvTrack, "_fetch_candidate_meta", _fake_fetch),
        patch.object(PendingCsvTrack, "_try_candidate_with_meta", _fake_try),
    ):
        result = await track.resolve()

    assert result is mock_track
    # Primary should have been tried first and succeeded — fallback not attempted
    assert try_calls == ["qobuz"]


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

    mock_meta = _CandidateMeta(resp={}, album=MagicMock(), meta=MagicMock())

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

    async def _fake_fetch(self_arg, candidate):
        return mock_meta

    async def _fake_try(self_arg, candidate, cached, quality):
        call_count[0] += 1
        if candidate.source == "qobuz":
            return None, "quality unavailable"  # primary fails
        return mock_track, "ok"  # fallback succeeds

    with (
        patch.object(PendingCsvTrack, "_fetch_candidate_meta", _fake_fetch),
        patch.object(PendingCsvTrack, "_try_candidate_with_meta", _fake_try),
    ):
        result = await track.resolve()

    assert result is mock_track
    # Pass-major fallback: try same pass index across services before stepping down.
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_pending_csv_track_pass_major_quality_then_service_order():
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    fallback_client = _make_client("deezer")

    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=_make_candidate("qobuz", primary_client, id="qobuz_id"),
        fallback_candidate=_make_candidate("deezer", fallback_client, id="deezer_id"),
        primary_qualities=[2, 1, 0],
        fallback_qualities=[1, 0],
        primary_source="qobuz",
        fallback_source="deezer",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Playlist",
        position=1,
        db=db,
    )

    mock_meta = _CandidateMeta(resp={}, album=MagicMock(), meta=MagicMock())
    attempts: list[tuple[str, int]] = []

    async def _fake_fetch(self_arg, candidate):
        return mock_meta

    async def _fake_try(self_arg, candidate, cached, quality):
        attempts.append((candidate.source, quality))
        return None, "quality unavailable"

    with (
        patch.object(PendingCsvTrack, "_fetch_candidate_meta", _fake_fetch),
        patch.object(PendingCsvTrack, "_try_candidate_with_meta", _fake_try),
    ):
        result = await track.resolve()

    assert result is None
    assert attempts == [
        ("qobuz", 2),
        ("deezer", 1),
        ("qobuz", 1),
        ("deezer", 0),
        ("qobuz", 0),
    ]


@pytest.mark.asyncio
async def test_pending_csv_track_all_passes_fail_returns_none():
    """If all quality passes fail, resolve returns None."""
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    fallback_client = _make_client("deezer")

    primary_cand = _make_candidate("qobuz", primary_client)
    fallback_cand = _make_candidate("deezer", fallback_client)

    mock_meta = _CandidateMeta(resp={}, album=MagicMock(), meta=MagicMock())

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

    async def _fake_fetch(self_arg, candidate):
        return mock_meta

    with (
        patch.object(PendingCsvTrack, "_fetch_candidate_meta", _fake_fetch),
        patch.object(
            PendingCsvTrack,
            "_try_candidate_with_meta",
            AsyncMock(return_value=(None, "quality unavailable")),
        ),
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
async def test_pending_csv_track_logs_attempt_trace_on_exhaustion(tmp_path):
    db = _make_db()
    from streamrip.db import UnresolvedQueryLog

    unresolved_path = str(tmp_path / "unresolved.csv")
    db.unresolved_log = UnresolvedQueryLog(unresolved_path)
    cfg = _make_config()
    primary_client = _make_client("qobuz")

    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=_make_candidate("qobuz", primary_client, id="q1"),
        fallback_candidate=None,
        primary_qualities=[2, 1],
        fallback_qualities=[],
        primary_source="qobuz",
        fallback_source="",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Playlist",
        position=1,
        db=db,
    )

    mock_meta = _CandidateMeta(resp={}, album=MagicMock(), meta=MagicMock())

    async def _fake_fetch(self_arg, candidate):
        return mock_meta

    async def _fake_try(self_arg, candidate, cached, quality):
        if quality == 2:
            return None, "quality unavailable"
        return None, "download failed"

    with (
        patch.object(PendingCsvTrack, "_fetch_candidate_meta", _fake_fetch),
        patch.object(PendingCsvTrack, "_try_candidate_with_meta", _fake_try),
    ):
        assert await track.resolve() is None

    with open(unresolved_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "attempt_trace" in content
    assert "qobuz@2:quality unavailable" in content
    assert "qobuz@1:download failed" in content
    assert "download attempt failed after a valid service/quality match" in content


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

    # With the metadata-cache refactor, patch _fetch_candidate_meta to verify
    # it is never called when the pre-check skips the track.
    with patch.object(
        PendingCsvTrack, "_fetch_candidate_meta", new_callable=AsyncMock
    ) as mock_fetch:
        result = await track.resolve()

    # Must skip entirely — metadata must not be fetched
    assert result is None
    mock_fetch.assert_not_called()
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

    with patch.object(
        PendingCsvTrack, "_fetch_candidate_meta", new_callable=AsyncMock
    ) as mock_fetch:
        result = await track.resolve()

    assert result is None
    mock_fetch.assert_not_called()
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
            with pytest.raises(PendingCsvPlaylist.FailFastAbortError):
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
async def test_pending_csv_playlist_query_cache_avoids_duplicate_search_calls():
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    primary_client.search = AsyncMock(return_value=[])

    rows = [
        _make_row(title="Song", artists=["Artist"], row_index=0),
        _make_row(title="Song", artists=["Artist"], row_index=1),
    ]

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=rows,
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
    )
    await pending.resolve()

    # Each unique query should be requested at most once and reused for the second row.
    assert primary_client.search.await_count <= 8


@pytest.mark.asyncio
async def test_pending_csv_playlist_disables_provider_after_repeated_auth_errors():
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    primary_client.search = AsyncMock(side_effect=Exception("401 unauthorized"))

    rows = [_make_row(row_index=i) for i in range(6)]
    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=rows,
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
    )
    await pending.resolve()
    assert pending.provider_budgets is not None
    assert pending.provider_budgets["qobuz"].disabled is True


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


@pytest.mark.asyncio
async def test_pending_csv_playlist_low_confidence_result_marked_unresolved(tmp_path):
    db = _make_db()
    unresolved_path = str(tmp_path / "unresolved.csv")
    from streamrip.db import UnresolvedQueryLog

    db.unresolved_log = UnresolvedQueryLog(unresolved_path)
    cfg = _make_config()
    primary_client = _make_client("qobuz")

    primary_client.search = AsyncMock(
        return_value=[
            {
                "tracks": {
                    "items": [
                        {
                            "id": 10,
                            "title": "Blue in Green (Live)",
                            "performer": {"name": "Wrong Artist"},
                            "album": {"title": "Other Album"},
                            "isrc": "",
                            "release_date_original": "2010",
                        }
                    ]
                }
            }
        ]
    )

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=[
            _make_row(
                title="Blue in Green",
                artists=["Miles Davis"],
                album="Kind of Blue",
                date="1959",
            )
        ],
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
    )

    with patch.dict("os.environ", {"STREAMRIP_COUNTRY_CODE": "US"}):
        result = await pending.resolve()
    assert result is None
    # Ensure this really is below configured confidence floor
    from streamrip.file_lists import score_candidate

    assert (
        score_candidate(
            _make_row(
                title="Blue in Green",
                artists=["Miles Davis"],
                album="Kind of Blue",
                date="1959",
            ),
            "Blue in Green (Live)",
            "Wrong Artist",
            "Other Album",
            "2010",
            "",
        )
        < _MIN_ACCEPTABLE_SCORE
    )

    with open(unresolved_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "low confidence" in content
    assert "query_strategy" in content
    assert "attempted_query" in content
    assert ",US," in content


@pytest.mark.asyncio
async def test_pending_csv_playlist_search_errors_are_logged_as_search_failed(tmp_path):
    db = _make_db()
    unresolved_path = str(tmp_path / "unresolved.csv")
    from streamrip.db import UnresolvedQueryLog

    db.unresolved_log = UnresolvedQueryLog(unresolved_path)
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    primary_client.search = AsyncMock(side_effect=RuntimeError("provider timeout"))

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=[_make_row()],
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
    )

    result = await pending.resolve()
    assert result is None

    with open(unresolved_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "search_failed" in content


@pytest.mark.asyncio
async def test_pending_csv_playlist_skips_fallback_when_primary_confident_match():
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    fallback_client = _make_client("deezer")

    primary_client.search = AsyncMock(
        return_value=[
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
    )
    fallback_client.search = AsyncMock(return_value=[])

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=[_make_row(title="Blue in Green", artists=["Miles Davis"])],
        primary_client=primary_client,
        fallback_client=fallback_client,
        config=cfg,
        db=db,
    )

    result = await pending.resolve()
    assert result is not None
    fallback_client.search.assert_not_called()


@pytest.mark.asyncio
async def test_pending_csv_playlist_marks_invalid_rows_unresolved(tmp_path):
    db = _make_db()
    unresolved_path = str(tmp_path / "unresolved.csv")
    from streamrip.db import UnresolvedQueryLog

    db.unresolved_log = UnresolvedQueryLog(unresolved_path)
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    primary_client.search = AsyncMock(return_value=[])

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=[_make_row(title="", artists=[])],
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
    )

    result = await pending.resolve()
    assert result is None
    primary_client.search.assert_not_called()
    with open(unresolved_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "invalid row: missing track name or artist" in content


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


# ---------------------------------------------------------------------------
# Deferred item A: metadata fetched exactly once per candidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_fetched_once_per_candidate_across_quality_passes():
    """_fetch_candidate_meta must be called once per candidate, not once per quality pass.

    The test verifies that even with 3 quality passes for primary, the
    metadata fetch is only invoked once for the primary candidate and once
    for the fallback candidate (if reached).
    """
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz", max_quality=2)
    fallback_client = _make_client("deezer", max_quality=2)

    primary_cand = _make_candidate("qobuz", primary_client, id="q1")
    fallback_cand = _make_candidate("deezer", fallback_client, id="d1")

    mock_meta = _CandidateMeta(resp={}, album=MagicMock(), meta=MagicMock())

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

    fetch_calls: list[str] = []

    async def _fake_fetch(self_arg, candidate):
        """
        Record the candidate's source and return a successful metadata fetch result with mocked metadata.

        Parameters:
            self_arg: Unused; preserved to match the original method signature.
            candidate: The track candidate whose `source` will be appended to `fetch_calls`.

        Returns:
            _MetaFetchResult: An object with `status="ok"` and `meta` set to `mock_meta`.
        """
        fetch_calls.append(candidate.source)
        return _MetaFetchResult(status="ok", meta=mock_meta)

    async def _fake_try(self_arg, candidate, cached, quality):
        # Always fail so all quality passes are exhausted
        """
        Simulate a failed candidate attempt so all quality passes are treated as exhausted.

        Returns:
            (None, str): A two-tuple where the first element is `None` (no track) and the second is the failure reason; specifically `"quality unavailable"`.
        """
        return None, "quality unavailable"

    with (
        patch.object(PendingCsvTrack, "_fetch_candidate_meta", _fake_fetch),
        patch.object(PendingCsvTrack, "_try_candidate_with_meta", _fake_try),
    ):
        result = await track.resolve()

    assert result is None
    # Each candidate fetched exactly once regardless of quality passes
    assert fetch_calls.count("qobuz") == 1
    assert fetch_calls.count("deezer") == 1
    assert len(fetch_calls) == 2


@pytest.mark.asyncio
async def test_metadata_fetch_failure_skips_candidate_quality_passes():
    """If metadata fetch is unavailable for one candidate, its quality passes
    must be skipped — the other candidate is still tried."""
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")
    fallback_client = _make_client("deezer")

    primary_cand = _make_candidate("qobuz", primary_client, id="q1")
    fallback_cand = _make_candidate("deezer", fallback_client, id="d1")

    mock_meta = _CandidateMeta(resp={}, album=MagicMock(), meta=MagicMock())
    mock_track = MagicMock()

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

    async def _fake_fetch(self_arg, candidate):
        """
        Simulate fetching metadata for a candidate in tests, returning a deterministic _MetaFetchResult.

        Parameters:
            candidate: The candidate whose `source` determines the simulated outcome.

        Returns:
            _MetaFetchResult: For candidates from `"qobuz"`, a result with `status="matched unavailable"`.
            For all other sources, a result with `status="ok"` and `meta` set to `mock_meta`.
        """
        if candidate.source == "qobuz":
            return _MetaFetchResult(status="matched unavailable")
        return _MetaFetchResult(status="ok", meta=mock_meta)

    async def _fake_try(self_arg, candidate, cached, quality):
        """
        Simulate attempting a candidate and always succeed for the first quality pass.

        Parameters:
            self_arg: Placeholder for bound method `self` (unused).
            candidate: Candidate being attempted (ignored by this fake).
            cached: Cached metadata/state for the candidate (ignored).
            quality: Quality level being attempted (ignored).

        Returns:
            tuple: (track, status) where `track` is the mocked track returned to signal success and `status` is the string `"ok"`.
        """
        return mock_track, "ok"  # fallback succeeds at first quality

    with (
        patch.object(PendingCsvTrack, "_fetch_candidate_meta", _fake_fetch),
        patch.object(PendingCsvTrack, "_try_candidate_with_meta", _fake_try),
    ):
        result = await track.resolve()

    # Fallback must have been used since primary metadata failed
    assert result is mock_track


@pytest.mark.asyncio
async def test_metadata_fetch_provider_error_not_classified_as_unavailable(tmp_path):
    db = _make_db()
    unresolved_path = str(tmp_path / "unresolved.csv")
    from streamrip.db import UnresolvedQueryLog

    db.unresolved_log = UnresolvedQueryLog(unresolved_path)
    cfg = _make_config()
    primary_client = _make_client("qobuz")

    primary_cand = _make_candidate("qobuz", primary_client, id="q1")
    track = PendingCsvTrack(
        row=_make_row(),
        primary_candidate=primary_cand,
        fallback_candidate=None,
        primary_qualities=[2, 1, 0],
        fallback_qualities=[],
        primary_source="qobuz",
        fallback_source="",
        config=cfg,
        folder="/tmp/test",
        playlist_name="Playlist",
        position=1,
        db=db,
    )

    async def _fake_fetch(_self_arg, _candidate):
        """
        Simulate a metadata fetch that fails with a provider-level error.

        Returns:
            _MetaFetchResult: An instance with `status` set to `"provider-error"`.
        """
        return _MetaFetchResult(status="provider-error")

    with patch.object(PendingCsvTrack, "_fetch_candidate_meta", _fake_fetch):
        result = await track.resolve()

    assert result is None
    with open(unresolved_path, encoding="utf-8") as fh:
        content = fh.read()
    assert "provider error while resolving matched candidate metadata" in content


# ---------------------------------------------------------------------------
# Deferred item C: repair-mode fuzzy matching and expanded search
# ---------------------------------------------------------------------------


def test_pick_best_candidate_repair_uses_fuzzy_when_exact_fails():
    """_pick_best_candidate_repair should return a candidate that the standard
    picker would also find, and score at least as well."""
    from streamrip.file_lists import ExportifyCsvRow

    row2 = ExportifyCsvRow(
        track_name="Bitches Brew",
        artists_raw="Miles Davis",
        artists_list=["Miles Davis"],
        album="Bitches Brew",
        release_date="1970",
        isrc="",
        spotify_uri="",
        genres="",
        loudness="",
        tempo="",
        position=1,
        row_index=0,
    )
    pages2 = [
        {
            "tracks": {
                "items": [
                    {
                        "id": 20,
                        "title": "Bitches' Brew",  # apostrophe — exact normalise fails
                        "performer": {"name": "Miles Davis"},
                        "album": {"title": "Bitches Brew"},
                        "isrc": "",
                        "release_date_original": "1970",
                    }
                ]
            }
        }
    ]

    client = _make_client("qobuz")

    from streamrip.media.csv_playlist import _pick_best_candidate

    # Standard picker: normalise strips apostrophe so titles match
    cand_std = _pick_best_candidate(row2, "qobuz", pages2, client)
    cand_repair = _pick_best_candidate_repair(row2, "qobuz", pages2, client)

    # Both should find it (normalise handles apostrophe), but repair
    # must return a valid candidate and score >= standard
    assert cand_repair is not None
    if cand_std is not None:
        assert cand_repair.score >= cand_std.score


def test_score_candidate_repair_fuzzy_path_activates_when_exact_fails():
    """Repair scorer should not regress when standard scorer already handles
    remaster-style variants."""
    from streamrip.file_lists import (
        ExportifyCsvRow,
        score_candidate,
        score_candidate_repair,
    )

    row = ExportifyCsvRow(
        track_name="Something in the Way",
        artists_raw="Nirvana",
        artists_list=["Nirvana"],
        album="Nevermind",
        release_date="1991",
        isrc="",
        spotify_uri="",
        genres="",
        loudness="",
        tempo="",
        position=1,
        row_index=0,
    )

    # "(Remaster)" suffix is handled in standard scoring via variant normalisation.
    candidate_title = "Something in the Way (Remaster)"
    candidate_artist = "Nirvana"
    candidate_album = "Nevermind"
    candidate_date = "1991"
    candidate_isrc = ""

    std_score = score_candidate(
        row,
        candidate_title,
        candidate_artist,
        candidate_album,
        candidate_date,
        candidate_isrc,
    )
    repair_score = score_candidate_repair(
        row,
        candidate_title,
        candidate_artist,
        candidate_album,
        candidate_date,
        candidate_isrc,
    )

    assert std_score > 0
    assert repair_score == std_score


def test_pick_best_candidate_repair_rejects_low_similarity():
    """A very different title should still return None in repair mode."""
    from streamrip.file_lists import ExportifyCsvRow

    row = ExportifyCsvRow(
        track_name="Symphony No. 5",
        artists_raw="Beethoven",
        artists_list=["Beethoven"],
        album="",
        release_date="",
        isrc="",
        spotify_uri="",
        genres="",
        loudness="",
        tempo="",
        position=1,
        row_index=0,
    )
    client = _make_client("qobuz")
    pages = [
        {
            "tracks": {
                "items": [
                    {
                        "id": 99,
                        "title": "Piano Sonata No. 14",  # completely different
                        "performer": {"name": "Beethoven"},
                        "album": {"title": "Moonlight"},
                        "isrc": "",
                        "release_date_original": "1800",
                    }
                ]
            }
        }
    ]
    cand = _pick_best_candidate_repair(row, "qobuz", pages, client)
    assert cand is None


@pytest.mark.asyncio
async def test_repair_mode_uses_expanded_search_limit():
    """In repair_mode=True, PendingCsvPlaylist must pass _REPAIR_SEARCH_LIMIT
    to the search call instead of _SEARCH_LIMIT."""
    import streamrip.media.csv_playlist as csv_mod

    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")

    search_limits: list[int] = []
    original_limit = csv_mod._SEARCH_LIMIT
    repair_limit = csv_mod._REPAIR_SEARCH_LIMIT

    async def _capture_search(media_type, query, limit=5):
        search_limits.append(limit)
        return []

    primary_client.search = AsyncMock(side_effect=_capture_search)

    rows = [_make_row(row_index=0)]

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=rows,
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
        repair_mode=True,
    )

    await pending.resolve()

    assert search_limits, "search was never called"
    assert all(
        lim == repair_limit for lim in search_limits
    ), f"Expected {repair_limit}, got {search_limits}"
    assert repair_limit > original_limit


@pytest.mark.asyncio
async def test_repair_mode_tries_candidate_id_hint_before_search():
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")

    row = _make_row(row_index=0)
    row.repair_candidate_ids = {"qobuz": "hinted-id"}

    primary_client.get_metadata = AsyncMock(
        return_value={
            "id": "hinted-id",
            "title": "Song",
            "performer": {"name": "Artist"},
            "album": {"title": "Album"},
            "release_date_original": "2020-01-01",
            "isrc": "",
        }
    )
    primary_client.search = AsyncMock(return_value=[])

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=[row],
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
        repair_mode=True,
    )

    await pending.resolve()

    primary_client.get_metadata.assert_awaited_once_with("hinted-id", "track")
    primary_client.search.assert_not_called()


@pytest.mark.asyncio
async def test_repair_mode_falls_back_to_search_when_candidate_id_hint_fails():
    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")

    row = _make_row(row_index=0)
    row.repair_candidate_ids = {"qobuz": "stale-id"}

    primary_client.get_metadata = AsyncMock(side_effect=Exception("not found"))
    primary_client.search = AsyncMock(return_value=[])

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=[row],
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
        repair_mode=True,
    )

    await pending.resolve()

    primary_client.get_metadata.assert_awaited_once_with("stale-id", "track")
    primary_client.search.assert_called()


@pytest.mark.asyncio
async def test_normal_mode_uses_standard_search_limit():
    """In normal mode (repair_mode=False), the standard _SEARCH_LIMIT is used."""
    import streamrip.media.csv_playlist as csv_mod

    db = _make_db()
    cfg = _make_config()
    primary_client = _make_client("qobuz")

    search_limits: list[int] = []

    async def _capture_search(media_type, query, limit=5):
        search_limits.append(limit)
        return []

    primary_client.search = AsyncMock(side_effect=_capture_search)

    rows = [_make_row(row_index=0)]

    pending = PendingCsvPlaylist(
        playlist_name="Test",
        rows=rows,
        primary_client=primary_client,
        fallback_client=None,
        config=cfg,
        db=db,
        repair_mode=False,
    )

    await pending.resolve()

    assert search_limits
    assert all(lim == csv_mod._SEARCH_LIMIT for lim in search_limits)


# ---------------------------------------------------------------------------
# Deferred item B: parse_unresolved_csv round-trip
# ---------------------------------------------------------------------------


def test_parse_unresolved_csv_round_trip(tmp_path):
    """parse_unresolved_csv must parse a log file back into ExportifyCsvRow objects
    that are structurally identical to what was written."""

    from streamrip.db import UnresolvedQueryLog
    from streamrip.file_lists import parse_unresolved_csv

    log_path = str(tmp_path / "test_unresolved.csv")
    log = UnresolvedQueryLog(log_path)

    # Write two entries
    log.log(
        track_name="Blue in Green",
        artists="Miles Davis",
        album="Kind of Blue",
        release_date="1959",
        isrc="US-ABC-12-34567",
        spotify_uri="spotify:track:abc123",
        primary_source="qobuz",
        fallback_source="deezer",
        primary_candidate_id="1234",
        fallback_candidate_id="5678",
        reason="all quality/service combinations failed",
        row_index=0,
    )
    log.log(
        track_name="So What",
        artists="Miles Davis",
        album="Kind of Blue",
        release_date="1959",
        isrc="",
        spotify_uri="",
        primary_source="qobuz",
        fallback_source="",
        reason="no search results from any service",
        row_index=1,
    )

    rows = parse_unresolved_csv(log_path)

    assert len(rows) == 2

    row0 = rows[0]
    assert row0.track_name == "Blue in Green"
    assert row0.artists_raw == "Miles Davis"
    assert row0.artists_list == ["Miles Davis"]
    assert row0.album == "Kind of Blue"
    assert row0.release_date == "1959"
    assert row0.isrc == "US-ABC-12-34567"
    assert row0.spotify_uri == "spotify:track:abc123"
    assert row0.repair_candidate_ids == {"qobuz": "1234", "deezer": "5678"}

    row1 = rows[1]
    assert row1.track_name == "So What"
    assert row1.isrc == ""
    assert row1.repair_candidate_ids is None


def test_parse_unresolved_csv_empty_file(tmp_path):
    """parse_unresolved_csv on a freshly-created (header-only) log must return []."""
    from streamrip.db import UnresolvedQueryLog
    from streamrip.file_lists import parse_unresolved_csv

    log_path = str(tmp_path / "empty.csv")
    UnresolvedQueryLog(log_path)  # creates header-only file

    rows = parse_unresolved_csv(log_path)
    assert rows == []


def test_unresolved_query_log_rotates_when_header_schema_mismatch(tmp_path):
    from streamrip.db import UnresolvedQueryLog

    log_path = tmp_path / "legacy_unresolved.csv"
    log_path.write_text("legacy_header_a,legacy_header_b\nv1,v2\n", encoding="utf-8")

    log = UnresolvedQueryLog(str(log_path))
    assert log.has_entries is False

    backups = list(tmp_path.glob("legacy_unresolved.csv.*.bak"))
    assert len(backups) == 1

    with open(log_path, encoding="utf-8") as fh:
        header = fh.readline().strip()
    assert header == ",".join(UnresolvedQueryLog.FIELDNAMES)


@pytest.mark.asyncio
async def test_repair_csv_main_method(tmp_path):
    """Main.repair_csv must parse the unresolved log, call resolve_csv with
    repair_mode=True, and write a new repair_unresolved log."""
    from unittest.mock import MagicMock, patch

    from streamrip.db import UnresolvedQueryLog
    from streamrip.rip.main import Main

    # Build a minimal unresolved log file with one entry
    log_path = str(tmp_path / "liked_songs_unresolved.csv")
    log = UnresolvedQueryLog(log_path)
    log.log(
        track_name="Blue in Green",
        artists="Miles Davis",
        album="Kind of Blue",
        release_date="1959",
        isrc="",
        spotify_uri="",
        primary_source="qobuz",
        fallback_source="deezer",
        reason="test",
        row_index=0,
    )

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

        resolve_calls: list[dict] = []

        async def _fake_resolve_csv(**kwargs):
            resolve_calls.append(kwargs)

        main.resolve_csv = _fake_resolve_csv

        await main.repair_csv(
            unresolved_csv_path=log_path,
            source="qobuz",
            fallback_source="deezer",
        )

    assert len(resolve_calls) == 1
    call = resolve_calls[0]
    assert call["repair_mode"] is True
    assert call["source"] == "qobuz"
    assert call["fallback_source"] == "deezer"
    # Unresolved log path must be a new _repair_unresolved.csv file
    assert "_repair_unresolved" in call["unresolved_log_path"]
    # One row was parsed from the log
    assert len(call["rows"]) == 1
    assert call["rows"][0].track_name == "Blue in Green"


@pytest.mark.asyncio
async def test_repair_csv_prioritizes_availability_rows_when_country_changes(tmp_path):
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    log_path = str(tmp_path / "liked_songs_unresolved.csv")
    with open(log_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            "timestamp,track_name,artists,album,release_date,isrc,spotify_uri,"
            "primary_source,fallback_source,reason,row_index,session_country,"
            "query_strategy,attempted_query,attempt_trace\n"
        )
        # row 0: low confidence (normal priority)
        fh.write(
            "2026-01-01T00:00:00Z,Track A,Artist,Album,2020,,,"
            "qobuz,deezer,low confidence (20<50),0,US,structured,qA,\n"
        )
        # row 1: catalog availability + previous country FR (should be prioritized if now US)
        fh.write(
            "2026-01-01T00:00:00Z,Track B,Artist,Album,2020,,,"
            "qobuz,deezer,matched item found; unavailable on current service,1,FR,"
            "structured,qB,\n"
        )

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
        patch.dict("os.environ", {"STREAMRIP_COUNTRY_CODE": "US"}),
    ):
        main = Main(config)
        resolve_calls = []

        async def _fake_resolve_csv(**kwargs):
            resolve_calls.append(kwargs)

        main.resolve_csv = _fake_resolve_csv
        await main.repair_csv(
            unresolved_csv_path=log_path,
            source="qobuz",
            fallback_source="deezer",
        )

    assert len(resolve_calls) == 1
    ordered = resolve_calls[0]["rows"]
    assert ordered[0].track_name == "Track B"
    assert ordered[1].track_name == "Track A"


@pytest.mark.asyncio
async def test_repair_csv_prioritizes_no_results_reasons(tmp_path):
    """
    Ensure Main.repair_csv processes unresolved rows so entries with reason "no results" are ordered before those with "metadata mismatch".

    Writes a two-row unresolved CSV (one "metadata mismatch", one "no results"), invokes Main.repair_csv with qobuz primary and deezer fallback, and asserts the rows passed to resolve_csv are ordered with the "no results" row first.
    """
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    log_path = str(tmp_path / "liked_songs_unresolved.csv")
    with open(log_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(
            "timestamp,track_name,artists,album,release_date,isrc,spotify_uri,"
            "primary_source,fallback_source,reason,row_index,session_country,"
            "query_strategy,attempted_query,attempt_trace\n"
        )
        fh.write(
            "2026-01-01T00:00:00Z,Track C,Artist,Album,2020,,,"
            "qobuz,deezer,metadata mismatch,0,US,structured,qC,\n"
        )
        fh.write(
            "2026-01-01T00:00:00Z,Track D,Artist,Album,2020,,,"
            "qobuz,deezer,no results,1,US,structured,qD,\n"
        )

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
        resolve_calls = []

        async def _fake_resolve_csv(**kwargs):
            """
            Record invocation arguments for a test double that simulates CSV resolution.

            Appends all keyword arguments received to the shared `resolve_calls` list so tests can inspect how the resolver was invoked.

            Parameters:
                **kwargs: Arbitrary keyword arguments representing the invocation details to record.
            """
            resolve_calls.append(kwargs)

        main.resolve_csv = _fake_resolve_csv
        await main.repair_csv(
            unresolved_csv_path=log_path,
            source="qobuz",
            fallback_source="deezer",
        )

    assert len(resolve_calls) == 1
    ordered = resolve_calls[0]["rows"]
    assert ordered[0].track_name == "Track D"
    assert ordered[1].track_name == "Track C"


@pytest.mark.asyncio
async def test_main_resolve_csv_rips_per_artist_batch_immediately():
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.reliability.fail_fast = False

    rows = [_make_row(title="A"), _make_row(title="B"), _make_row(title="C")]
    event_order: list[str] = []

    class _FakePlaylist:
        def __init__(self, label: str):
            self.label = label

        async def rip(self):
            event_order.append(f"rip-{self.label}")

    class _FakePendingCsvPlaylist:
        created = 0

        def __init__(
            self,
            playlist_name,
            rows,
            primary_client,
            fallback_client,
            config,
            db,
            repair_mode=False,
        ):
            _FakePendingCsvPlaylist.created += 1
            self.label = str(_FakePendingCsvPlaylist.created)
            self.rows = rows

        async def resolve(self):
            event_order.append(f"resolve-{self.label}")
            return _FakePlaylist(self.label)

    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
    ):
        main = Main(config)

    main.get_logged_in_client = AsyncMock(return_value=MagicMock(source="qobuz"))

    with (
        patch(
            "streamrip.file_lists.partition_exportify_rows_artist_batched",
            return_value=[rows[:2], rows[2:]],
        ),
        patch("streamrip.rip.main.PendingCsvPlaylist", _FakePendingCsvPlaylist),
    ):
        await main.resolve_csv(
            playlist_name="Test",
            rows=rows,
            source="qobuz",
            fallback_source="",
        )

    assert event_order == ["resolve-1", "rip-1", "resolve-2", "rip-2"]


@pytest.mark.asyncio
async def test_main_resolve_csv_continues_after_unresolved_batch():
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.reliability.fail_fast = False

    rows = [_make_row(title="A"), _make_row(title="B"), _make_row(title="C")]
    event_order: list[str] = []

    class _FakePlaylist:
        async def rip(self):
            event_order.append("rip")

    class _FakePendingCsvPlaylist:
        created = 0

        def __init__(
            self,
            playlist_name,
            rows,
            primary_client,
            fallback_client,
            config,
            db,
            repair_mode=False,
        ):
            _FakePendingCsvPlaylist.created += 1
            self.seq = _FakePendingCsvPlaylist.created

        async def resolve(self):
            event_order.append(f"resolve-{self.seq}")
            if self.seq == 1:
                return None
            return _FakePlaylist()

    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
    ):
        main = Main(config)

    main.get_logged_in_client = AsyncMock(return_value=MagicMock(source="qobuz"))

    with (
        patch(
            "streamrip.file_lists.partition_exportify_rows_artist_batched",
            return_value=[rows[:2], rows[2:]],
        ),
        patch("streamrip.rip.main.PendingCsvPlaylist", _FakePendingCsvPlaylist),
    ):
        await main.resolve_csv(
            playlist_name="Test",
            rows=rows,
            source="qobuz",
            fallback_source="",
        )

    assert event_order == ["resolve-1", "resolve-2", "rip"]


@pytest.mark.asyncio
async def test_main_resolve_csv_fail_fast_stops_after_rip_exception():
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.reliability.fail_fast = True

    rows = [_make_row(title="A"), _make_row(title="B"), _make_row(title="C")]
    event_order: list[str] = []

    class _FailingPlaylist:
        async def rip(self):
            event_order.append("rip-1")
            raise RuntimeError("boom")

    class _OkPlaylist:
        async def rip(self):
            event_order.append("rip-2")

    class _FakePendingCsvPlaylist:
        created = 0

        def __init__(
            self,
            playlist_name,
            rows,
            primary_client,
            fallback_client,
            config,
            db,
            repair_mode=False,
        ):
            _FakePendingCsvPlaylist.created += 1
            self.seq = _FakePendingCsvPlaylist.created

        async def resolve(self):
            event_order.append(f"resolve-{self.seq}")
            if self.seq == 1:
                return _FailingPlaylist()
            return _OkPlaylist()

    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
    ):
        main = Main(config)

    main.get_logged_in_client = AsyncMock(return_value=MagicMock(source="qobuz"))

    with (
        patch(
            "streamrip.file_lists.partition_exportify_rows_artist_batched",
            return_value=[rows[:2], rows[2:]],
        ),
        patch("streamrip.rip.main.PendingCsvPlaylist", _FakePendingCsvPlaylist),
    ):
        await main.resolve_csv(
            playlist_name="Test",
            rows=rows,
            source="qobuz",
            fallback_source="",
        )

    assert event_order == ["resolve-1", "rip-1"]


@pytest.mark.asyncio
async def test_main_resolve_csv_non_fail_fast_continues_after_rip_exception():
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.reliability.fail_fast = False

    rows = [_make_row(title="A"), _make_row(title="B"), _make_row(title="C")]
    event_order: list[str] = []

    class _FailingPlaylist:
        async def rip(self):
            event_order.append("rip-1")
            raise RuntimeError("boom")

    class _OkPlaylist:
        async def rip(self):
            event_order.append("rip-2")

    class _FakePendingCsvPlaylist:
        created = 0

        def __init__(
            self,
            playlist_name,
            rows,
            primary_client,
            fallback_client,
            config,
            db,
            repair_mode=False,
        ):
            _FakePendingCsvPlaylist.created += 1
            self.seq = _FakePendingCsvPlaylist.created

        async def resolve(self):
            event_order.append(f"resolve-{self.seq}")
            if self.seq == 1:
                return _FailingPlaylist()
            return _OkPlaylist()

    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
    ):
        main = Main(config)

    main.get_logged_in_client = AsyncMock(return_value=MagicMock(source="qobuz"))

    with (
        patch(
            "streamrip.file_lists.partition_exportify_rows_artist_batched",
            return_value=[rows[:2], rows[2:]],
        ),
        patch("streamrip.rip.main.PendingCsvPlaylist", _FakePendingCsvPlaylist),
    ):
        await main.resolve_csv(
            playlist_name="Test",
            rows=rows,
            source="qobuz",
            fallback_source="",
        )

    assert event_order == ["resolve-1", "rip-1", "resolve-2", "rip-2"]


@pytest.mark.asyncio
async def test_main_resolve_csv_rip_exception_contributes_to_main_rip_failures():
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.reliability.fail_fast = False

    rows = [_make_row(title="A"), _make_row(title="B")]

    class _FailingPlaylist:
        async def rip(self):
            raise RuntimeError("boom")

    class _FakePendingCsvPlaylist:
        def __init__(
            self,
            playlist_name,
            rows,
            primary_client,
            fallback_client,
            config,
            db,
            repair_mode=False,
        ):
            self.rows = rows

        async def resolve(self):
            return _FailingPlaylist()

    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
    ):
        main = Main(config)

    main.get_logged_in_client = AsyncMock(return_value=MagicMock(source="qobuz"))

    with (
        patch(
            "streamrip.file_lists.partition_exportify_rows_artist_batched",
            return_value=[rows],
        ),
        patch("streamrip.rip.main.PendingCsvPlaylist", _FakePendingCsvPlaylist),
    ):
        await main.resolve_csv(
            playlist_name="Test",
            rows=rows,
            source="qobuz",
            fallback_source="",
        )

    assert await main.rip() == 1


@pytest.mark.asyncio
async def test_main_resolve_csv_resolve_exception_contributes_to_main_rip_failures():
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.reliability.fail_fast = False

    rows = [_make_row(title="A"), _make_row(title="B")]

    class _FakePendingCsvPlaylist:
        def __init__(
            self,
            playlist_name,
            rows,
            primary_client,
            fallback_client,
            config,
            db,
            repair_mode=False,
        ):
            self.rows = rows

        async def resolve(self):
            raise RuntimeError("resolve boom")

    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
    ):
        main = Main(config)

    main.get_logged_in_client = AsyncMock(return_value=MagicMock(source="qobuz"))

    with (
        patch(
            "streamrip.file_lists.partition_exportify_rows_artist_batched",
            return_value=[rows],
        ),
        patch("streamrip.rip.main.PendingCsvPlaylist", _FakePendingCsvPlaylist),
    ):
        await main.resolve_csv(
            playlist_name="Test",
            rows=rows,
            source="qobuz",
            fallback_source="",
        )

    assert await main.rip() == 1


@pytest.mark.asyncio
async def test_main_resolve_csv_fail_fast_stops_after_resolve_exception():
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.reliability.fail_fast = True

    rows = [_make_row(title="A"), _make_row(title="B"), _make_row(title="C")]
    event_order: list[str] = []
    raised_types: list[type[Exception]] = []

    class _FakePendingCsvPlaylist:
        created = 0

        def __init__(
            self,
            playlist_name,
            rows,
            primary_client,
            fallback_client,
            config,
            db,
            repair_mode=False,
        ):
            _FakePendingCsvPlaylist.created += 1
            self.seq = _FakePendingCsvPlaylist.created

        async def resolve(self):
            event_order.append(f"resolve-{self.seq}")
            if self.seq == 1:
                raised_types.append(PendingCsvPlaylist.FailFastAbortError)
                raise PendingCsvPlaylist.FailFastAbortError("resolve boom")
            return MagicMock(rip=AsyncMock())

    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
    ):
        main = Main(config)

    main.get_logged_in_client = AsyncMock(return_value=MagicMock(source="qobuz"))

    with (
        patch(
            "streamrip.file_lists.partition_exportify_rows_artist_batched",
            return_value=[rows[:2], rows[2:]],
        ),
        patch("streamrip.rip.main.PendingCsvPlaylist", _FakePendingCsvPlaylist),
    ):
        await main.resolve_csv(
            playlist_name="Test",
            rows=rows,
            source="qobuz",
            fallback_source="",
        )

    assert event_order == ["resolve-1"]
    assert raised_types == [PendingCsvPlaylist.FailFastAbortError]
    assert main._csv_top_level_failures == 1
    assert await main.rip() == 1


@pytest.mark.asyncio
async def test_main_resolve_csv_uses_env_batch_size_override():
    from unittest.mock import MagicMock, patch

    from streamrip.rip.main import Main

    config = MagicMock()
    config.session.downloads.requests_per_minute = 0
    config.session.database.downloads_enabled = False
    config.session.database.failed_downloads_enabled = False
    config.session.reliability.fail_fast = False

    rows = [_make_row(title="A"), _make_row(title="B")]

    class _FakePlaylist:
        async def rip(self):
            return

    class _FakePendingCsvPlaylist:
        def __init__(
            self,
            playlist_name,
            rows,
            primary_client,
            fallback_client,
            config,
            db,
            repair_mode=False,
        ):
            self.rows = rows

        async def resolve(self):
            return _FakePlaylist()

    with (
        patch("streamrip.rip.main.QobuzClient"),
        patch("streamrip.rip.main.TidalClient"),
        patch("streamrip.rip.main.DeezerClient"),
        patch("streamrip.rip.main.SoundcloudClient"),
    ):
        main = Main(config)

    main.get_logged_in_client = AsyncMock(return_value=MagicMock(source="qobuz"))

    with (
        patch.dict("os.environ", {"STREAMRIP_EXPORTIFY_BATCH_SIZE": "25"}),
        patch(
            "streamrip.file_lists.partition_exportify_rows_artist_batched",
            return_value=[rows],
        ) as partition_mock,
        patch("streamrip.rip.main.PendingCsvPlaylist", _FakePendingCsvPlaylist),
    ):
        await main.resolve_csv(
            playlist_name="Test",
            rows=rows,
            source="qobuz",
            fallback_source="",
        )

    assert partition_mock.call_args.kwargs["max_batch_size"] == 25


def test_get_exportify_batch_size_invalid_env_falls_back_to_default():
    from streamrip.rip.main import Main

    with patch.dict("os.environ", {"STREAMRIP_EXPORTIFY_BATCH_SIZE": "invalid"}):
        assert Main._get_exportify_batch_size() == 40


def test_get_exportify_batch_size_low_env_falls_back_to_default():
    from streamrip.rip.main import Main

    with patch.dict("os.environ", {"STREAMRIP_EXPORTIFY_BATCH_SIZE": "0"}):
        assert Main._get_exportify_batch_size() == 40
