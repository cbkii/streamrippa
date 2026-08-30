"""Exportify CSV playlist resolution for ``rip file --list-mode exportify-csv``.

Architecture
------------
:class:`PendingCsvPlaylist` is a :class:`Pending` whose ``resolve()``
method:

1. Runs a bounded batch search for every CSV row on both the primary and
   fallback services.
2. Scores each service's results with a lightweight deterministic scoring
   function (ISRC > title+artist > title+album > year > first).
3. Builds :class:`PendingCsvTrack` items that carry both candidates and
   their per-service descending quality sequences.
4. Returns a :class:`Playlist` of those pending tracks.

:class:`PendingCsvTrack.resolve` implements the service-first /
quality-second fallback algorithm:

- Pass 0: try primary at primary_qualities[0], then fallback at fallback_qualities[0]
- Pass 1: try primary at primary_qualities[1], then fallback at fallback_qualities[1]
- …until both quality sequences are exhausted.

Deezer downloads use ``exact_quality=True`` so that quality stepping happens
at the caller level rather than silently inside Deezer's client.

Unresolved rows (no candidate found from either service) are logged to
:class:`~streamrip.db.UnresolvedQueryLog` for audit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from time import monotonic

from rich.text import Text

from ..client import Client
from ..config import Config
from ..console import console
from ..db import CsvResolverTelemetryLog, Database
from ..exceptions import NonStreamableError
from ..file_lists import (
    CandidateExplanation,
    ExportifyCsvRow,
    MatchPolicy,
    _artist_overlap,
    _duration_match_with_tolerance,
    _normalise,
    explain_candidate_score,
    is_usable_exportify_row,
    score_candidate,
    score_candidate_repair,
    strip_title_decorators,
)
from ..filepath_utils import clean_filepath
from ..metadata import AlbumMetadata, Covers, TrackMetadata
from .artwork import download_artwork
from .media import Pending
from .playlist import Playlist
from .track import Track

logger = logging.getLogger("streamrip")

# Resolver batch size: cap concurrent search coroutines to avoid unbounded fan-out.
# Derived from a sensible maximum; deliberately not a user-facing config key.
_RESOLVER_BATCH_SIZE = 10

# Number of search results to fetch per service per row (normal import path)
_SEARCH_LIMIT = 8

# Expanded search window used in repair mode for heavier matching
_REPAIR_SEARCH_LIMIT = 15
_MIN_ACCEPTABLE_SCORE = 50
_MIN_ACCEPTABLE_SCORE_REPAIR = 50
_SHORTLIST_K = 3
_DEFAULT_LOW_SCORE_FLOOR = 25

REASON_NO_RESULTS = "no results"
REASON_SEARCH_FAILURE = "search failure"
REASON_LOW_CONFIDENCE = "low confidence"
REASON_QUALITY_UNAVAILABLE = "quality unavailable"
REASON_NO_RESULTS_AFTER_BROAD = "no-results-after-broad-search"
REASON_PROVIDER_SEARCH_ERROR = "provider-search-error"
REASON_AMBIGUOUS = "ambiguous-candidates"
REASON_TITLE_REJECTED = "candidates-found-but-title-rejected"
REASON_ARTIST_REJECTED = "candidates-found-but-artist-rejected"
REASON_VARIANT_CONFLICT = "variant-conflict"
REASON_DURATION_CONFLICT = "duration-conflict"
CONF_REJECT = "reject"
CONF_LOW = "low"
CONF_MEDIUM = "medium"
CONF_HIGH = "high"
TELEMETRY_SCHEMA_VERSION = "csv_resolver_outcome_v1"


@dataclass(slots=True)
class _ProviderBudget:
    search_sem: asyncio.Semaphore
    metadata_sem: asyncio.Semaphore
    url_sem: asyncio.Semaphore
    next_allowed_ts: float = 0.0
    cooldown_until_ts: float = 0.0
    failure_streak: int = 0
    cooldown_count: int = 0
    rate_limited_count: int = 0
    auth_error_count: int = 0
    disabled: bool = False


async def _budgeted_get_raw_metadata(
    candidate: "TrackCandidate",
    provider_budgets: "dict[str, _ProviderBudget] | None",
    provider_wait_fn: Callable[[str], Awaitable[None]],
    provider_after_call_fn: Callable[[str, bool, Exception | None], None],
) -> dict:
    """Fetch raw track metadata for *candidate* honouring provider semaphores and cooldowns.

    Acquires the per-source ``metadata_sem``, waits for any cooldown, then
    calls ``candidate.client.get_metadata``.  On success invokes
    ``provider_after_call_fn(source, ok=True, err=None)`` and returns the raw
    response dict.  On any exception invokes
    ``provider_after_call_fn(source, ok=False, err=e)`` and re-raises so the
    caller can handle specific exception types.
    """
    try:
        if provider_budgets is not None and candidate.source in provider_budgets:
            async with provider_budgets[candidate.source].metadata_sem:
                await provider_wait_fn(candidate.source)
                resp = await candidate.client.get_metadata(candidate.id, "track")
        else:
            resp = await candidate.client.get_metadata(candidate.id, "track")
        provider_after_call_fn(candidate.source, ok=True, err=None)
        return resp
    except Exception as e:
        provider_after_call_fn(candidate.source, ok=False, err=e)
        raise


def _session_country_hint() -> str:
    """Best-effort country hint for unresolved CSV diagnostics.

    This is intentionally lightweight/non-networked. If unset, returns empty.
    """
    return (os.getenv("STREAMRIP_COUNTRY_CODE") or "").strip().upper()


@dataclass(slots=True)
class TrackCandidate:
    """A resolved track candidate from a service search result."""

    source: str
    id: str
    title: str
    artist: str
    album: str
    release_date: str
    isrc: str
    score: int
    client: Client
    reason_codes: tuple[str, ...] = ()
    signals: dict[str, object] | None = None
    confidence: str = "reject"
    margin_to_second: int = 0


@dataclass(slots=True)
class ResolverOutcome:
    candidate: TrackCandidate | None
    reason: str
    query: str
    strategy: str
    rejected: list[TrackCandidate] | None = None
    attempts: tuple[dict[str, object], ...] = ()


def _build_quality_sequence(source: str, max_quality: int) -> list[int]:
    """Return a descending quality sequence for *source* starting from
    *max_quality* down to 0."""
    return list(range(max_quality, -1, -1))


def _extract_raw_results(source: str, pages: list[dict]) -> list[dict]:
    """Extract raw track item dicts from search response pages.

    Returns a flat list of raw API item dicts with at minimum the fields
    ``id``, ``title``/``name``, artist info, release date, and ``isrc``.
    """
    items: list[dict] = []
    for page in pages:
        if source == "deezer":
            items.extend(page.get("data", []))
        elif source == "qobuz":
            items.extend(page.get("tracks", {}).get("items", []))
        elif source == "tidal":
            items.extend(page.get("items", []))
        elif source == "soundcloud":
            items.extend(page.get("collection", []))
    return items


def _item_title(source: str, item: dict) -> str:
    return (item.get("title") or item.get("name") or "").strip()


def _item_artist(source: str, item: dict) -> str:
    if source == "deezer":
        return item.get("artist", {}).get("name", "")
    if source == "qobuz":
        return item.get("performer", {}).get("name", "")
    if source == "tidal":
        artists = item.get("artists") or []
        if artists:
            return ", ".join(a.get("name", "") for a in artists)
        return item.get("artist", {}).get("name", "")
    if source == "soundcloud":
        return item.get("user", {}).get("username", "")
    return ""


def _item_album(source: str, item: dict) -> str:
    if source == "deezer":
        return item.get("album", {}).get("title", "")
    if source == "qobuz":
        return item.get("album", {}).get("title", "")
    if source == "tidal":
        return item.get("album", {}).get("title", "")
    return ""


def _item_date(source: str, item: dict) -> str:
    def _first_nonempty(*values: str | None) -> str:
        for value in values:
            if value:
                return str(value)
        return ""

    if source == "deezer":
        return _first_nonempty(
            item.get("recording_date"),
            item.get("release_date"),
            item.get("album", {}).get("recording_date"),
            item.get("album", {}).get("release_date"),
        )
    if source == "qobuz":
        return _first_nonempty(
            item.get("release_date_original"),
            item.get("album", {}).get("release_date_original"),
            item.get("release_date"),
            item.get("album", {}).get("release_date"),
        )
    if source == "tidal":
        return _first_nonempty(
            item.get("streamStartDate"),
            item.get("album", {}).get("releaseDate"),
            item.get("copyrightYear"),
        )
    return ""


def _item_isrc(source: str, item: dict) -> str:
    return (item.get("isrc") or "").strip()


def _item_duration_ms(source: str, item: dict) -> int | None:
    value = (
        item.get("duration")
        or item.get("duration_ms")
        or item.get("durationMillis")
        or item.get("duration_msec")
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    # deezer/qobuz search duration tends to be seconds
    if parsed < 10000:
        parsed *= 1000
    return parsed if parsed > 0 else None


def _first_artist(row: ExportifyCsvRow) -> str:
    if row.artists_list:
        return row.artists_list[0]
    return row.artists_raw


def _normalize_local_lookup_text(value: str) -> str:
    value = strip_title_decorators(value or "")
    # Fold separators/punctuation conservatively to reduce formatting variance
    # without broadening matching into wildcard behavior.
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[/|+]+", " ", value)
    value = re.sub(r"['`]", "", value)
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def _row_local_lookup_keys(row: ExportifyCsvRow) -> list[str]:
    title = _normalize_local_lookup_text(row.track_name)
    stripped = _normalize_local_lookup_text(row.canonical_track_name or row.track_name)
    artist = _normalize_local_lookup_text(_first_artist(row))
    album = _normalize_local_lookup_text(row.album)
    keys = []
    for maybe in (title, stripped):
        if maybe:
            if artist and album:
                keys.append(f"{maybe}::{artist}::{album}")
            if artist:
                keys.append(f"{maybe}::{artist}")
            if album:
                keys.append(f"{maybe}::{album}")
            keys.append(maybe)
    # stable de-dupe
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _file_local_lookup_keys(path: Path) -> list[str]:
    stem = _normalize_local_lookup_text(path.stem)
    parent = _normalize_local_lookup_text(path.parent.name)
    if not stem:
        return []
    keys = [stem]
    if parent:
        keys.append(f"{stem}::{parent}")
    return keys


_BROAD_SEARCH_STRATEGIES: frozenset[str] = frozenset(
    {"title-only", "canonical-title-only"}
)


def _build_search_queries(
    row: ExportifyCsvRow, source: str, *, escalation: bool = False
) -> list[tuple[str, str]]:
    """Build a deterministic adaptive query plan from strong to broad identity.

    Album and release year remain scoring evidence rather than mandatory terms
    in every provider query.  Broad title-only discovery is available in normal
    mode, but candidates still pass the same strict acceptance scorer.
    """
    first_artist = _first_artist(row)
    canonical_title = row.canonical_track_name or strip_title_decorators(row.track_name)

    queries: list[tuple[str, str]] = []
    if row.isrc and source in {"deezer", "qobuz"}:
        queries.append(("isrc", row.isrc))

    if row.album:
        queries.append(
            (
                "structured",
                " ".join(p for p in (row.track_name, first_artist, row.album) if p),
            )
        )
        if canonical_title and canonical_title != row.track_name:
            queries.append(
                (
                    "stripped-structured",
                    " ".join(
                        p for p in (canonical_title, first_artist, row.album) if p
                    ),
                )
            )

    queries.append(
        ("generic", " ".join(p for p in (row.track_name, first_artist) if p))
    )
    if canonical_title and canonical_title != row.track_name:
        queries.append(
            (
                "stripped-generic",
                " ".join(p for p in (canonical_title, first_artist) if p),
            )
        )

    queries.append(
        ("artist-title", " ".join(p for p in (first_artist, row.track_name) if p))
    )
    if canonical_title and canonical_title != row.track_name:
        queries.append(
            (
                "artist-canonical-title",
                " ".join(p for p in (first_artist, canonical_title) if p),
            )
        )

    if row.album:
        queries.append(
            ("title-album", " ".join(p for p in (row.track_name, row.album) if p))
        )
        if canonical_title and canonical_title != row.track_name:
            queries.append(
                (
                    "canonical-title-album",
                    " ".join(p for p in (canonical_title, row.album) if p),
                )
            )

    # Broad discovery is normal-mode capable.  Acceptance remains strict.
    queries.append(("title-only", row.track_name))
    if canonical_title and canonical_title != row.track_name:
        queries.append(("canonical-title-only", canonical_title))

    if escalation and row.album:
        queries.append(
            (
                "album-title-artist",
                " ".join(p for p in (row.album, row.track_name, first_artist) if p),
            )
        )

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for strategy, query in queries:
        qn = " ".join(query.split())
        key = qn.casefold()
        if qn and key not in seen:
            seen.add(key)
            out.append((strategy, qn))
    return out


def _select_best_candidate(
    hits: list[tuple[str, str, TrackCandidate]],
) -> tuple[str, str, TrackCandidate] | None:
    """Select the strongest discovered candidate while preserving tie order."""
    if not hits:
        return None
    confidence_rank = {CONF_REJECT: 0, CONF_LOW: 1, CONF_MEDIUM: 2, CONF_HIGH: 3}
    _, best = max(
        enumerate(hits),
        key=lambda item: (
            item[1][2].score,
            confidence_rank.get(item[1][2].confidence, 0),
            -item[0],
        ),
    )
    return best


def _rejection_reason(candidate: TrackCandidate | None) -> str:
    if candidate is None:
        return REASON_NO_RESULTS
    reasons = set(candidate.reason_codes)
    if "reject_title_mismatch" in reasons:
        return REASON_TITLE_REJECTED
    if "reject_artist_mismatch" in reasons or "reject_isrc_artist_conflict" in reasons:
        return REASON_ARTIST_REJECTED
    if "reject_variant_policy" in reasons or "reject_isrc_variant_conflict" in reasons:
        return REASON_VARIANT_CONFLICT
    if "reject_duration_far" in reasons or "reject_isrc_duration_conflict" in reasons:
        return REASON_DURATION_CONFLICT
    if "reject_bad_context" in reasons:
        return "bad-context-conflict"
    return "candidates-found-but-rejected"


def _pick_best_candidate(
    row: ExportifyCsvRow,
    source: str,
    pages: list[dict],
    client: Client,
    policy: MatchPolicy | None = None,
) -> TrackCandidate | None:
    """Score all results from *pages* and return the best :class:`TrackCandidate`.

    Returns ``None`` if *pages* is empty.
    """
    items = _extract_raw_results(source, pages)
    if not items:
        return None

    best_item = None
    best_score = -1

    effective_policy = policy or MatchPolicy()
    for item in items:
        title = _item_title(source, item)
        artist = _item_artist(source, item)
        album = _item_album(source, item)
        date = _item_date(source, item)
        isrc = _item_isrc(source, item)

        sc = score_candidate(
            row,
            title,
            artist,
            album,
            date,
            isrc,
            _item_duration_ms(source, item),
            policy=effective_policy,
        )
        if sc > best_score:
            best_score = sc
            best_item = item

    if best_item is None or best_score <= 0:
        return None

    return TrackCandidate(
        source=source,
        id=str(best_item["id"]),
        title=_item_title(source, best_item),
        artist=_item_artist(source, best_item),
        album=_item_album(source, best_item),
        release_date=_item_date(source, best_item),
        isrc=_item_isrc(source, best_item),
        score=best_score,
        client=client,
    )


def _pick_best_candidate_repair(
    row: ExportifyCsvRow,
    source: str,
    pages: list[dict],
    client: Client,
    policy: MatchPolicy | None = None,
) -> TrackCandidate | None:
    """Legacy repair helper retained for compatibility/tests.

    The active resolver path uses :func:`_pick_top_candidates`; this function
    remains as a small adapter for tests and compatibility.
    """
    items = _extract_raw_results(source, pages)
    if not items:
        return None

    best_item = None
    best_score = -1

    effective_policy = policy or MatchPolicy()
    for item in items:
        title = _item_title(source, item)
        artist = _item_artist(source, item)
        album = _item_album(source, item)
        date = _item_date(source, item)
        isrc = _item_isrc(source, item)

        sc = score_candidate_repair(
            row,
            title,
            artist,
            album,
            date,
            isrc,
            _item_duration_ms(source, item),
            policy=effective_policy,
        )
        if sc > best_score:
            best_score = sc
            best_item = item

    if best_item is None or best_score <= 0:
        return None

    return TrackCandidate(
        source=source,
        id=str(best_item["id"]),
        title=_item_title(source, best_item),
        artist=_item_artist(source, best_item),
        album=_item_album(source, best_item),
        release_date=_item_date(source, best_item),
        isrc=_item_isrc(source, best_item),
        score=best_score,
        client=client,
    )


def _candidate_reason(candidate: TrackCandidate | None, min_score: int) -> str:
    if candidate is None:
        return REASON_NO_RESULTS
    if candidate.score < min_score:
        return f"{REASON_LOW_CONFIDENCE} ({candidate.score}<{min_score})"
    return "matched"


def _confidence_for_candidate(
    candidate: TrackCandidate | None, min_score: int, margin_to_second: int
) -> str:
    if candidate is None or candidate.score < min_score:
        return CONF_REJECT
    if candidate.score >= min_score + 20 and margin_to_second >= 8:
        return CONF_HIGH
    if candidate.score >= min_score + 10 and margin_to_second >= 4:
        return CONF_MEDIUM
    return CONF_LOW


def _provider_threshold(csv_cfg, source: str, *, repair_mode: bool) -> int:
    default_threshold = (
        _MIN_ACCEPTABLE_SCORE_REPAIR if repair_mode else _MIN_ACCEPTABLE_SCORE
    )
    raw_map = getattr(csv_cfg, "acceptance_threshold_by_source", {}) if csv_cfg else {}
    if isinstance(raw_map, dict):
        raw = raw_map.get(source, default_threshold)
        if isinstance(raw, bool):
            logger.warning(
                "Invalid csv_resolver.acceptance_threshold_by_source value for '%s': %r; using default=%d",
                source,
                raw,
                default_threshold,
            )
            return default_threshold
        try:
            threshold = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid csv_resolver.acceptance_threshold_by_source value for '%s': %r; using default=%d",
                source,
                raw,
                default_threshold,
            )
            return default_threshold
        if 0 <= threshold <= 100:
            return threshold
        logger.warning(
            "Out-of-range csv_resolver.acceptance_threshold_by_source value for '%s': %r (must be 0-100); using default=%d",
            source,
            raw,
            default_threshold,
        )
        return default_threshold
    if raw_map:
        logger.warning(
            "Invalid csv_resolver.acceptance_threshold_by_source type %s; using defaults",
            type(raw_map).__name__,
        )
    return default_threshold


def _serialize_rejected_candidate(candidate: TrackCandidate) -> str:
    return json.dumps(
        {
            "source": candidate.source,
            "id": candidate.id,
            "title": candidate.title,
            "artist": candidate.artist,
            "album": candidate.album,
            "date": candidate.release_date,
            "isrc": candidate.isrc,
            "score": candidate.score,
            "reason_codes": list(candidate.reason_codes),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _candidate_payload(c: TrackCandidate | None) -> dict | None:
    if c is None:
        return None
    return {
        "source": c.source,
        "id": c.id,
        "title": c.title,
        "artist": c.artist,
        "album": c.album,
        "date": c.release_date,
        "isrc": c.isrc,
        "score": c.score,
        "confidence": c.confidence,
        "margin_to_second": c.margin_to_second,
        "reason_codes": list(c.reason_codes),
        "signals": c.signals or {},
    }


def _rejected_candidate_payloads(
    candidates: list[TrackCandidate] | None,
) -> list[dict]:
    serialized: list[dict] = []
    for candidate in candidates or []:
        payload = _candidate_payload(candidate)
        if payload is not None:
            serialized.append(payload)
    return serialized


def _margin_to_second_best(score: int, population_scores: list[int]) -> int:
    if not population_scores:
        return 0
    ranked = sorted(population_scores, reverse=True)
    best = ranked[0]
    if score < best:
        return 0
    second = ranked[1] if len(ranked) > 1 else 0
    return max(0, score - second)


def _pick_top_candidates(
    row: ExportifyCsvRow,
    source: str,
    pages: list[dict],
    client: Client,
    *,
    repair_mode: bool,
    limit: int = _SHORTLIST_K,
    policy: MatchPolicy | None = None,
) -> list[TrackCandidate]:
    items = _extract_raw_results(source, pages)
    if not items:
        return []
    scored: list[TrackCandidate] = []
    active_policy = policy or MatchPolicy()
    seen_ids: set[str] = set()
    for item in items:
        item_id = str(item.get("id", "")).strip()
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        title = _item_title(source, item)
        artist = _item_artist(source, item)
        album = _item_album(source, item)
        date = _item_date(source, item)
        isrc = _item_isrc(source, item)
        duration_ms = _item_duration_ms(source, item)
        explain: CandidateExplanation | None = None
        if repair_mode:
            score = score_candidate_repair(
                row,
                title,
                artist,
                album,
                date,
                isrc,
                duration_ms,
                policy=active_policy,
            )
        else:
            explain = explain_candidate_score(
                row,
                title,
                artist,
                album,
                date,
                isrc,
                duration_ms,
                policy=active_policy,
            )
            score = explain.score
        if score <= 0:
            continue
        scored.append(
            TrackCandidate(
                source=source,
                id=item_id,
                title=title,
                artist=artist,
                album=album,
                release_date=date,
                isrc=isrc,
                score=score,
                client=client,
                reason_codes=explain.reason_codes if explain is not None else (),
                signals=explain.signals if explain is not None else None,
            )
        )
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]


def _top_rejected_candidates(
    row: ExportifyCsvRow,
    source: str,
    pages: list[dict],
    client: Client,
    *,
    policy: MatchPolicy | None = None,
    limit: int = 2,
) -> list[TrackCandidate]:
    items = _extract_raw_results(source, pages)
    active_policy = policy or MatchPolicy()
    rejected: list[TrackCandidate] = []
    seen_ids: set[str] = set()
    for item in items:
        item_id = str(item.get("id", "")).strip()
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        title = _item_title(source, item)
        artist = _item_artist(source, item)
        album = _item_album(source, item)
        date = _item_date(source, item)
        isrc = _item_isrc(source, item)
        duration_ms = _item_duration_ms(source, item)
        explain = explain_candidate_score(
            row,
            title,
            artist,
            album,
            date,
            isrc,
            duration_ms,
            policy=active_policy,
        )
        if explain.score > 0:
            continue
        rejected.append(
            TrackCandidate(
                source=source,
                id=item_id,
                title=title,
                artist=artist,
                album=album,
                release_date=date,
                isrc=isrc,
                score=explain.score,
                client=client,
                reason_codes=explain.reason_codes,
                signals=explain.signals,
            )
        )
    # All rejected candidates have score == 0.  Sort by title-similarity to the
    # row as a proxy for "closeness", so the most relevant rejections surface
    # first in telemetry and unresolved logs.
    row_norm = _normalise(row.track_name)

    def _proximity(c: TrackCandidate) -> float:
        return SequenceMatcher(None, row_norm, _normalise(c.title)).ratio()

    rejected.sort(key=_proximity, reverse=True)
    return rejected[:limit]


def _build_extra_tags(
    row: ExportifyCsvRow,
    provider_genre: str | None,
    tag_map: dict,
) -> dict | None:
    """Build the ``extra_tags`` dict from a CSV row and the configured tag map.

    The ``"genre"`` target is treated specially: CSV genres are merged with
    the provider genre, de-duplicated while preserving deterministic order.

    Returns ``None`` when the map is empty or all mapped values are blank.
    """
    if not tag_map:
        return None

    # Map from CSV column name to the row value
    csv_values: dict[str, str] = {
        "Genres": row.genres,
        "Loudness": row.loudness,
        "Tempo": row.tempo,
        "Track URI": row.spotify_uri,
        "Track Name": row.track_name,
        "Artist Name(s)": row.artists_raw,
        "Album Name": row.album,
        "Release Date": row.release_date,
        "ISRC": row.isrc,
    }

    extra: dict[str, str] = {}

    for csv_col, target_tag in tag_map.items():
        value = csv_values.get(csv_col, "").strip()
        if not value:
            continue

        if target_tag.lower() == "genre":
            # Merge CSV genres with provider genre (de-duplicate, stable order)
            csv_genres = [g.strip() for g in value.split(",") if g.strip()]
            provider_genres = (
                [g.strip() for g in provider_genre.split(",") if g.strip()]
                if provider_genre
                else []
            )
            seen: set[str] = set()
            merged: list[str] = []
            for g in provider_genres + csv_genres:
                lower_g = g.lower()
                if lower_g not in seen:
                    seen.add(lower_g)
                    merged.append(g)
            if merged:
                extra[target_tag] = ", ".join(merged)
        else:
            extra[target_tag] = value

    return extra if extra else None


@dataclass
class _CandidateMeta:
    """Cached metadata for a single candidate — fetched once, reused across quality passes."""

    resp: dict
    album: AlbumMetadata
    meta: TrackMetadata


@dataclass(slots=True)
class _MetaFetchResult:
    status: str
    meta: _CandidateMeta | None = None


@dataclass(slots=True)
class AttemptResult:
    source: str
    quality: int
    status: str


@dataclass(slots=True)
class PendingCsvTrack(Pending):
    """A track derived from an Exportify CSV row, ready to resolve and download.

    Holds pre-searched candidates from both the primary and fallback services
    and implements the service-first / quality-second fallback algorithm.

    Metadata for each candidate is fetched **once** before the quality loop and
    cached in ``_primary_meta`` / ``_fallback_meta`` so that repeated quality
    passes for the same candidate do not incur repeated API calls.
    """

    row: ExportifyCsvRow
    primary_candidate: TrackCandidate | None
    fallback_candidate: TrackCandidate | None
    # Descending quality sequence for each service (e.g. [2, 1, 0])
    primary_qualities: list[int]
    fallback_qualities: list[int]
    primary_source: str
    fallback_source: str
    config: Config
    folder: str
    playlist_name: str
    position: int
    db: Database
    provider_budgets: dict[str, _ProviderBudget] | None = None
    negative_candidate_cache: dict[tuple[str, str], str] | None = None

    async def _provider_wait(self, source: str) -> None:
        if self.provider_budgets is None:
            return
        budget = self.provider_budgets.get(source)
        if budget is None:
            return
        wait_for = max(budget.cooldown_until_ts, budget.next_allowed_ts) - monotonic()
        if wait_for > 0:
            await asyncio.sleep(wait_for)

    def _provider_after_call(
        self, source: str, ok: bool, err: Exception | None
    ) -> None:
        if self.provider_budgets is None:
            return
        budget = self.provider_budgets.get(source)
        if budget is None:
            return
        csv_cfg = getattr(self.config.session, "csv_resolver", None)
        min_interval = float(
            getattr(csv_cfg, "provider_min_interval_seconds", 0.2) or 0
        )
        budget.next_allowed_ts = max(budget.next_allowed_ts, monotonic()) + min_interval
        if ok:
            budget.failure_streak = 0
            return
        budget.failure_streak += 1

    async def resolve(self) -> Track | None:
        """Attempt to download the track using service-first / quality-second logic.

        Returns a :class:`Track` on success, or ``None`` if no combination
        succeeded (the failure is logged).

        Metadata for each candidate is fetched exactly once before the quality
        loop, so multiple quality passes for the same candidate never repeat
        the metadata API call.
        """
        # Pre-check: if *either* candidate is already in the downloads DB, skip the
        # whole track immediately.  This prevents the fallback service from re-
        # downloading a track that was already obtained via the primary service (or
        # vice-versa) in a previous run.  The inner check inside ``_try_candidate``
        # remains as a safety net for concurrent same-session downloads.
        candidates = [
            c
            for c in (self.primary_candidate, self.fallback_candidate)
            if c is not None
        ]
        for cand in candidates:
            if self.db.downloaded(cand.id, source=cand.source):
                logger.info(
                    "Track %s:%s already downloaded. Skipping '%s' by %s.",
                    cand.source,
                    cand.id,
                    self.row.track_name,
                    self.row.artists_raw,
                )
                self.db.set_skipped()
                return None

        # --- Fetch metadata for each candidate exactly once ---
        primary_fetch = _MetaFetchResult(status="no-candidate")
        fallback_fetch = _MetaFetchResult(status="no-candidate")

        if self.primary_candidate is not None:
            primary_fetch = await self._fetch_candidate_meta(self.primary_candidate)

        if self.fallback_candidate is not None:
            fallback_fetch = await self._fetch_candidate_meta(self.fallback_candidate)

        attempts: list[AttemptResult] = []

        effective_primary = (
            self.primary_candidate
            if self.primary_candidate is not None and primary_fetch.meta is not None
            else None
        )
        effective_fallback = (
            self.fallback_candidate
            if self.fallback_candidate is not None and fallback_fetch.meta is not None
            else None
        )

        if self.primary_candidate is not None and primary_fetch.meta is None:
            attempts.append(
                AttemptResult(self.primary_source, -1, primary_fetch.status)
            )
        if self.fallback_candidate is not None and fallback_fetch.meta is None:
            attempts.append(
                AttemptResult(self.fallback_source, -1, fallback_fetch.status)
            )

        max_passes = max(
            len(self.primary_qualities) if effective_primary else 0,
            len(self.fallback_qualities) if effective_fallback else 0,
        )

        # Pass-major fallback (quality-prioritised): each pass tries the same
        # quality-step index across configured services before stepping down.
        for pass_idx in range(max_passes):
            if effective_primary and pass_idx < len(self.primary_qualities):
                assert primary_fetch.meta is not None
                quality = self.primary_qualities[pass_idx]
                track, status = await self._try_candidate_with_meta(
                    effective_primary,
                    primary_fetch.meta,
                    quality,
                )
                attempts.append(
                    AttemptResult(effective_primary.source, quality, status)
                )
                if track is not None:
                    logger.info(
                        "Resolved '%s' via %s at quality %d",
                        self.row.track_name,
                        effective_primary.source,
                        quality,
                    )
                    return track

            if effective_fallback and pass_idx < len(self.fallback_qualities):
                assert fallback_fetch.meta is not None
                quality = self.fallback_qualities[pass_idx]
                track, status = await self._try_candidate_with_meta(
                    effective_fallback,
                    fallback_fetch.meta,
                    quality,
                )
                attempts.append(
                    AttemptResult(effective_fallback.source, quality, status)
                )
                if track is not None:
                    logger.info(
                        "Resolved '%s' via %s at quality %d",
                        self.row.track_name,
                        effective_fallback.source,
                        quality,
                    )
                    return track

        # All passes exhausted
        reason = self._classify_failure(attempts)

        logger.warning(
            "Could not download '%s' by %s (%s)",
            self.row.track_name,
            self.row.artists_raw,
            reason,
        )

        if self.db.unresolved_log is not None:
            self.db.unresolved_log.log(
                track_name=self.row.track_name,
                artists=self.row.artists_raw,
                album=self.row.album,
                release_date=self.row.release_date,
                isrc=self.row.isrc,
                spotify_uri=self.row.spotify_uri,
                primary_source=self.primary_source,
                fallback_source=self.fallback_source,
                primary_candidate_id=(
                    self.primary_candidate.id if self.primary_candidate else ""
                ),
                fallback_candidate_id=(
                    self.fallback_candidate.id if self.fallback_candidate else ""
                ),
                reason=reason,
                row_index=self.row.row_index,
                source_row_index=self.row.source_row_index,
                original_position=self.row.position,
                duration_ms=self.row.duration_ms,
                session_country=_session_country_hint(),
                attempt_trace=" | ".join(
                    f"{a.source}@{a.quality}:{a.status}" for a in attempts
                ),
            )

        return None

    @staticmethod
    def _classify_failure(attempts: list[AttemptResult]) -> str:
        """
        Classify a list of attempt results into a human-readable failure category.

        Parameters:
            attempts (list[AttemptResult]): Sequence of attempted (service, quality) attempts with their `status` strings.

        Returns:
            str: A failure category describing why resolution/download did not succeed. Possible categories include:
            - "all configured service/quality combinations exhausted"
            - "matched item found, but unavailable on current service"
            - "download attempt failed after a valid service/quality match"
            - "matched item available, but requested/highest quality unavailable"
            - "provider error while resolving matched candidate metadata"
            - "metadata processing error for matched candidate"
            - "matched candidate already downloaded in concurrent run"
        """
        if not attempts:
            return "all configured service/quality combinations exhausted"
        statuses = {a.status for a in attempts}
        if "matched unavailable" in statuses and not (
            "download failed" in statuses or "provider-error" in statuses
        ):
            return "matched item found, but unavailable on current service"
        if "download failed" in statuses:
            return "download attempt failed after a valid service/quality match"
        if "quality unavailable" in statuses:
            return REASON_QUALITY_UNAVAILABLE
        if "provider-error" in statuses:
            return "provider error while resolving matched candidate metadata"
        if "metadata-error" in statuses:
            return "metadata processing error for matched candidate"
        if "duplicate-race" in statuses:
            return "matched candidate already downloaded in concurrent run"
        return "all configured service/quality combinations exhausted"

    async def _fetch_candidate_meta(
        self,
        candidate: TrackCandidate,
    ) -> _MetaFetchResult:
        """
        Fetch and assemble metadata for a single track candidate and return a structured outcome status.

        Performs a single metadata retrieval and constructs album and track metadata; callers should cache the result and reuse it across quality attempts. The returned _MetaFetchResult.status is one of:
        - "ok": metadata successfully fetched and parsed; `_MetaFetchResult.meta` contains the constructed `_CandidateMeta`.
        - "duplicate-race": candidate was already recorded as downloaded in the database; no metadata returned.
        - "provider-error": the provider request failed or raised an unexpected error; no metadata returned.
        - "matched unavailable": the provider response indicates the track or album is not streamable/available; no metadata returned.
        - "metadata-error": the provider response could not be converted into required TrackMetadata; no metadata returned.

        On success, the returned `_MetaFetchResult.meta` holds:
        - `resp`: raw provider response,
        - `album`: `AlbumMetadata` built from the response,
        - `meta`: `TrackMetadata` built from the response (may include configuration-driven adjustments and best-effort extra tags).
        """
        # Source-aware duplicate check (inner safety net for concurrent downloads)
        if self.db.downloaded(candidate.id, source=candidate.source):
            logger.info(
                "Track %s:%s already in database. Skipping.",
                candidate.source,
                candidate.id,
            )
            self.db.set_skipped()
            return _MetaFetchResult(status="duplicate-race")

        cache_key = (candidate.source, candidate.id)
        if self.negative_candidate_cache is not None:
            cached_status = self.negative_candidate_cache.get(cache_key)
            if cached_status in {
                "matched unavailable",
                "provider-error",
                "metadata-error",
            }:
                return _MetaFetchResult(status=cached_status)

        try:
            resp = await _budgeted_get_raw_metadata(
                candidate,
                self.provider_budgets,
                self._provider_wait,
                self._provider_after_call,
            )
        except NonStreamableError as e:
            logger.debug(
                "Could not fetch metadata for %s:%s: %s",
                candidate.source,
                candidate.id,
                e,
            )
            if self.negative_candidate_cache is not None:
                self.negative_candidate_cache[cache_key] = "matched unavailable"
            return _MetaFetchResult(status="matched unavailable")
        except Exception as e:
            logger.debug(
                "Unexpected error fetching metadata for %s:%s: %s",
                candidate.source,
                candidate.id,
                e,
            )
            if self.negative_candidate_cache is not None:
                self.negative_candidate_cache[cache_key] = "provider-error"
            return _MetaFetchResult(status="provider-error")

        album = AlbumMetadata.from_track_resp(resp, candidate.source)
        if album is None:
            logger.debug(
                "Track %s:%s not available on %s",
                candidate.source,
                candidate.id,
                candidate.source,
            )
            if self.negative_candidate_cache is not None:
                self.negative_candidate_cache[cache_key] = "matched unavailable"
            return _MetaFetchResult(status="matched unavailable")

        meta = TrackMetadata.from_resp(album, candidate.source, resp)
        if meta is None:
            logger.debug(
                "Could not build TrackMetadata for %s:%s",
                candidate.source,
                candidate.id,
            )
            if self.negative_candidate_cache is not None:
                self.negative_candidate_cache[cache_key] = "metadata-error"
            return _MetaFetchResult(status="metadata-error")

        c = self.config.session.metadata
        if c.renumber_playlist_tracks:
            meta.tracknumber = self.position
        if c.set_playlist_to_album:
            album.album = self.playlist_name

        # Build extra tags from CSV row (best-effort)
        tag_map = getattr(c, "exportify_tag_map", {})
        if tag_map:
            try:
                provider_genre = album.get_genres() if album.genre else None
                meta.extra_tags = _build_extra_tags(
                    self.row, provider_genre, dict(tag_map)
                )
            except Exception as e:
                logger.warning("Failed to build extra tags for '%s': %s", meta.title, e)

        return _MetaFetchResult(
            status="ok",
            meta=_CandidateMeta(resp=resp, album=album, meta=meta),
        )

    async def _try_candidate_with_meta(
        self,
        candidate: TrackCandidate,
        cached: _CandidateMeta,
        quality: int,
    ) -> tuple[Track | None, str]:
        """
        Try to produce a downloadable Track for the given candidate using already-fetched metadata.

        Parameters:
            candidate (TrackCandidate): Candidate identifying source and track id.
            cached (_CandidateMeta): Cached per-candidate metadata (album, track metadata, raw response) previously obtained.
            quality (int): Desired quality step to request from the provider.

        Returns:
            tuple[Track | None, str]: A pair of (track, status). `track` is a Track object on success, `None` otherwise.
            `status` is one of:
              - `"ok"`: downloadable obtained and Track constructed,
              - `"quality unavailable"`: provider indicated the requested quality cannot be streamed,
              - `"download failed"`: other error occurred while obtaining the downloadable.
        """
        # Attempt download at the requested quality.
        # Pass exact_quality=True for Deezer so the caller controls stepping.
        try:

            async def _get_downloadable():
                if (
                    self.provider_budgets is not None
                    and candidate.source in self.provider_budgets
                ):
                    async with self.provider_budgets[candidate.source].url_sem:
                        await self._provider_wait(candidate.source)
                        if candidate.source == "deezer":
                            return await candidate.client.get_downloadable(
                                candidate.id, quality, exact_quality=True
                            )
                        return await candidate.client.get_downloadable(
                            candidate.id, quality
                        )
                if candidate.source == "deezer":
                    return await candidate.client.get_downloadable(
                        candidate.id, quality, exact_quality=True
                    )
                return await candidate.client.get_downloadable(candidate.id, quality)

            embedded_cover_path, downloadable = await asyncio.gather(
                self._download_cover(cached.album.covers, candidate.client),
                _get_downloadable(),
                return_exceptions=True,
            )
            if isinstance(downloadable, Exception):
                raise downloadable
            if isinstance(embedded_cover_path, Exception):
                logger.warning(
                    "Could not download cover for %s:%s: %s",
                    candidate.source,
                    candidate.id,
                    embedded_cover_path,
                )
                embedded_cover_path = None
            self._provider_after_call(candidate.source, ok=True, err=None)
        except NonStreamableError as e:
            self._provider_after_call(candidate.source, ok=False, err=e)
            logger.debug(
                "Quality %d not available for %s:%s: %s",
                quality,
                candidate.source,
                candidate.id,
                e,
            )
            return None, "quality unavailable"
        except Exception as e:
            self._provider_after_call(candidate.source, ok=False, err=e)
            logger.debug(
                "Error getting downloadable for %s:%s: %s",
                candidate.source,
                candidate.id,
                e,
            )
            return None, "download failed"

        return (
            Track(
                cached.meta,
                downloadable,
                self.config,
                self.folder,
                embedded_cover_path,
                self.db,
                expected_duration_ms=self.row.duration_ms,
            ),
            "ok",
        )

    async def _download_cover(self, covers: Covers, client: Client) -> str | None:
        embed_path, _ = await download_artwork(
            client.session,
            self.folder,
            covers,
            self.config.session.artwork,
            for_playlist=True,
        )
        return embed_path


@dataclass(slots=True)
class PendingCsvPlaylist(Pending):
    """Resolves a list of :class:`ExportifyCsvRow` items into a :class:`Playlist`.

    Runs searches on both *primary_client* and *fallback_client* (if present)
    in bounded batches, scores the results, and yields :class:`PendingCsvTrack`
    items that implement per-pass quality-priority over source:
    primary@quality[i] -> fallback@quality[i] -> next lower quality.

    When ``repair_mode=True`` the resolver uses an expanded search window
    (:data:`_REPAIR_SEARCH_LIMIT`) and fuzzy title scoring
    (:func:`_pick_best_candidate_repair`) to recover rows that the lightweight
    main-path scorer left unresolved. Optional guarded low-score acceptance is
    available only in repair mode.
    """

    playlist_name: str
    rows: list[ExportifyCsvRow]
    primary_client: Client
    fallback_client: Client | None
    config: Config
    db: Database
    repair_mode: bool = False
    accept_lowscore: bool = False
    lowscore_floor: int = _DEFAULT_LOW_SCORE_FLOOR
    query_cache: dict[tuple[str, str, int], list[dict]] | None = None
    provider_budgets: dict[str, _ProviderBudget] | None = None
    negative_candidate_cache: dict[tuple[str, str], str] | None = None
    local_file_index: dict[str, list[Path]] | None = None
    local_duration_cache: dict[str, float | None] | None = None
    telemetry_log: CsvResolverTelemetryLog | None = None
    local_skipped_count: int = 0
    low_score_accepted_count: int = 0
    unresolved_count: int = 0

    @dataclass(slots=True)
    class Status:
        found: int
        failed: int
        unresolved: int
        local_skipped: int
        low_score_accepted: int
        total: int

        def text(self) -> Text:
            return Text.assemble(
                "Resolving CSV tracks (",
                (f"{self.found} found", "bold green"),
                ", ",
                (f"{self.failed} failed", "bold red"),
                ", ",
                (f"{self.unresolved} unresolved", "bold yellow"),
                ", ",
                (f"{self.local_skipped} local-skip", "bold cyan"),
                ", ",
                (f"{self.low_score_accepted} low-score accepted", "bold magenta"),
                ", ",
                (f"{self.total} total", "bold"),
                ")",
            )

    class FailFastAbortError(RuntimeError):
        """Raised when fail-fast mode aborts CSV row resolution for a batch."""

    def _csv_cfg(self):
        return getattr(self.config.session, "csv_resolver", None)

    def _telemetry(self) -> CsvResolverTelemetryLog:
        if self.telemetry_log is None:
            csv_cfg = self._csv_cfg()
            path = (
                str(getattr(csv_cfg, "telemetry_jsonl_path", "") or "")
                if csv_cfg is not None
                else ""
            )
            self.telemetry_log = CsvResolverTelemetryLog(path)
        return self.telemetry_log

    def _get_duration_tolerance(self) -> tuple[float, float]:
        csv_cfg = self._csv_cfg()
        ratio_val = getattr(csv_cfg, "local_skip_duration_tolerance_ratio", None)
        ratio = float(ratio_val) if ratio_val is not None else 0.20
        seconds_val = getattr(csv_cfg, "local_skip_duration_tolerance_seconds", None)
        seconds = float(seconds_val) if seconds_val is not None else 12.0
        return max(0.0, ratio), max(0.0, seconds)

    def _build_local_index(self) -> None:
        """Build one deterministic local-file lookup index for this playlist run."""
        if self.local_file_index is not None:
            return
        csv_cfg = self._csv_cfg()
        if not bool(getattr(csv_cfg, "local_skip_enabled", False)):
            return

        configured_paths = list(getattr(csv_cfg, "local_skip_paths", []) or [])
        roots = configured_paths or [self.config.session.downloads.folder]
        extensions = {
            str(ext).strip().casefold().lstrip(".")
            for ext in (getattr(csv_cfg, "local_skip_extensions", []) or [])
            if str(ext).strip()
        }
        if not extensions:
            extensions = {"flac", "mp3", "m4a", "ogg", "opus", "aac"}

        max_scan = int(getattr(csv_cfg, "local_skip_max_file_scan", 25000) or 25000)
        max_scan = max(1, max_scan)

        index: dict[str, list[Path]] = {}
        scanned = 0
        for root in sorted({str(Path(p).expanduser()) for p in roots if p}):
            if scanned >= max_scan:
                break
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                filenames.sort()
                for name in filenames:
                    if scanned >= max_scan:
                        break
                    path = Path(dirpath) / name
                    if path.suffix.casefold().lstrip(".") not in extensions:
                        continue
                    scanned += 1
                    for key in _file_local_lookup_keys(path):
                        index.setdefault(key, []).append(path)
                if scanned >= max_scan:
                    break

        for key in index:
            index[key] = sorted(index[key], key=lambda p: str(p))
        self.local_file_index = index
        if self.local_duration_cache is None:
            self.local_duration_cache = {}
        logger.info(
            "CSV local-skip index built: %d keys from %d files (cap=%d)",
            len(index),
            scanned,
            max_scan,
        )

    def _duration_for_local_path(self, path: Path) -> float | None:
        if self.local_duration_cache is None:
            self.local_duration_cache = {}
        key = str(path)
        if key in self.local_duration_cache:
            return self.local_duration_cache[key]
        try:
            from mutagen import File as MutagenFile
            from mutagen import MutagenError

            info = MutagenFile(str(path))
            seconds = float(getattr(getattr(info, "info", None), "length", 0.0))
            self.local_duration_cache[key] = seconds if seconds > 0 else None
        except (MutagenError, OSError, ValueError) as e:
            logger.warning("local-skip duration read failed for '%s': %s", path, e)
            self.local_duration_cache[key] = None
        return self.local_duration_cache[key]

    def _try_local_skip(self, row: ExportifyCsvRow) -> tuple[bool, str, str]:
        csv_cfg = self._csv_cfg()
        if self.local_file_index is None:
            return False, "", "local-skip-disabled"
        require_duration = bool(
            getattr(csv_cfg, "local_skip_require_duration_check", True)
        )
        ratio, seconds = self._get_duration_tolerance()
        saw_ambiguous = False
        saw_duration_mismatch = False
        for key in _row_local_lookup_keys(row):
            matches = sorted({str(p) for p in self.local_file_index.get(key, [])})
            if len(matches) > 1:
                saw_ambiguous = True
                continue
            for path in matches:
                if require_duration:
                    if not row.duration_ms:
                        # Duration check required but CSV row has no duration field:
                        # don't accept as a local skip to respect user intent.
                        continue
                    actual = self._duration_for_local_path(Path(path))
                    if not _duration_match_with_tolerance(
                        row.duration_ms,
                        actual,
                        tolerance_ratio=ratio,
                        tolerance_seconds=seconds,
                    ):
                        saw_duration_mismatch = True
                        continue
                    return True, path, "duration-validated-local"
                return True, path, "exact-local"
        if saw_ambiguous:
            return False, "", "ambiguous-local-not-skipped"
        if saw_duration_mismatch:
            return False, "", "duration-mismatch-local-not-skipped"
        return False, "", "no-local-match"

    def _init_provider_budgets(self) -> None:
        csv_cfg = self._csv_cfg()

        def _coerce_int(value, default: int) -> int:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return default
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def _int_cfg(name: str, default: int) -> int:
            val = _coerce_int(getattr(csv_cfg, name, default), default)
            return max(1, val)

        search_limit = _int_cfg("search_inflight_per_provider", 3)
        metadata_limit = _int_cfg("metadata_inflight_per_provider", 2)
        url_limit = _int_cfg("url_inflight_per_provider", 2)
        self.provider_budgets = {
            src: _ProviderBudget(
                search_sem=asyncio.Semaphore(search_limit),
                metadata_sem=asyncio.Semaphore(metadata_limit),
                url_sem=asyncio.Semaphore(url_limit),
            )
            for src in {
                self.primary_client.source,
                self.fallback_client.source if self.fallback_client is not None else "",
            }
            if src
        }

    async def _provider_wait(self, source: str) -> None:
        if self.provider_budgets is None:
            return
        budget = self.provider_budgets[source]
        now = monotonic()
        wait_for = max(budget.cooldown_until_ts, budget.next_allowed_ts) - now
        if wait_for > 0:
            await asyncio.sleep(wait_for)

    def _provider_after_call(
        self, source: str, ok: bool, err: Exception | None
    ) -> None:
        if self.provider_budgets is None:
            return
        budget = self.provider_budgets[source]
        csv_cfg = self._csv_cfg()

        def _coerce_float(value, default: float) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def _coerce_int(value, default: int) -> int:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return default
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        min_interval = max(
            0.0,
            _coerce_float(getattr(csv_cfg, "provider_min_interval_seconds", 0.2), 0.2),
        )
        budget.next_allowed_ts = max(budget.next_allowed_ts, monotonic()) + min_interval
        if ok:
            budget.failure_streak = 0
            return
        budget.failure_streak += 1
        msg = str(err).lower() if err is not None else ""
        if "429" in msg or "too many" in msg:
            budget.rate_limited_count += 1
        if (
            "401" in msg
            or "unauthorized" in msg
            or re.search(r"\bauth(?:entication)?\b", msg, re.IGNORECASE)
        ):
            budget.auth_error_count += 1
            if budget.auth_error_count >= 3:
                budget.disabled = True
        threshold = _coerce_int(getattr(csv_cfg, "failure_streak_for_cooldown", 4), 4)
        if threshold <= 0:
            return
        if budget.failure_streak < threshold:
            return
        base = max(
            0.0, _coerce_float(getattr(csv_cfg, "cooldown_base_seconds", 10.0), 10.0)
        )
        cap = max(
            0.0, _coerce_float(getattr(csv_cfg, "cooldown_max_seconds", 120.0), 120.0)
        )
        cooldown = min(cap, base * (2 ** max(budget.cooldown_count, 0)))
        cooldown += random.uniform(0.0, 1.0)
        budget.cooldown_until_ts = monotonic() + cooldown
        budget.cooldown_count += 1
        budget.failure_streak = 0

    async def resolve(self) -> Playlist | None:
        if self.query_cache is None:
            self.query_cache = {}
        if self.negative_candidate_cache is None:
            self.negative_candidate_cache = {}
        if self.provider_budgets is None:
            self._init_provider_budgets()
        parent = self.config.session.downloads.folder
        folder = os.path.join(parent, clean_filepath(self.playlist_name))

        status = self.Status(0, 0, 0, 0, 0, len(self.rows))
        fail_fast = self.config.session.reliability.fail_fast
        self._build_local_index()

        primary_qualities = _build_quality_sequence(
            self.primary_client.source,
            self.config.session.get_source(self.primary_client.source).quality,
        )
        fallback_qualities: list[int] = []
        if self.fallback_client is not None:
            fallback_qualities = _build_quality_sequence(
                self.fallback_client.source,
                self.config.session.get_source(self.fallback_client.source).quality,
            )

        # Batch-resolve all rows with bounded concurrency
        pending_tracks: list[PendingCsvTrack] = []

        show_progress = self.config.session.cli.progress_bars

        def _handle_batch_results(results):
            """Process asyncio.gather results; return True to stop (fail_fast)."""
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Batch resolver error: %s", result)
                    status.failed += 1
                    if fail_fast:
                        return True  # signal stop
                elif result is not None:
                    pending_tracks.append(result)
            return False

        if show_progress:
            with console.status(status.text(), spinner="moon") as st:

                async def _update():
                    st.update(status.text())

                async def _resolve_batch(batch):
                    coros = [
                        self._resolve_row(
                            row,
                            folder,
                            primary_qualities,
                            fallback_qualities,
                            status,
                            _update,
                        )
                        for row in batch
                    ]
                    return await asyncio.gather(*coros, return_exceptions=True)

                for batch in _chunks(self.rows, _RESOLVER_BATCH_SIZE):
                    results = await _resolve_batch(batch)
                    if _handle_batch_results(results):
                        message = "fail_fast: stopping CSV resolver after batch error."
                        logger.warning(message)
                        raise self.FailFastAbortError(message)
        else:

            async def _resolve_row_plain(row):
                return await self._resolve_row(
                    row,
                    folder,
                    primary_qualities,
                    fallback_qualities,
                    status,
                    None,
                )

            for batch in _chunks(self.rows, _RESOLVER_BATCH_SIZE):
                coros = [_resolve_row_plain(row) for row in batch]
                results = await asyncio.gather(*coros, return_exceptions=True)
                if _handle_batch_results(results):
                    message = "fail_fast: stopping CSV resolver after batch error."
                    logger.warning(message)
                    raise self.FailFastAbortError(message)

        logger.info(
            "CSV resolve complete: %d found, %d failed, %d unresolved, %d local-skipped, %d low-score accepted out of %d rows",
            status.found,
            status.failed,
            status.unresolved,
            status.local_skipped,
            status.low_score_accepted,
            status.total,
        )
        self.local_skipped_count = status.local_skipped
        self.low_score_accepted_count = status.low_score_accepted
        self.unresolved_count = status.unresolved

        if not pending_tracks:
            logger.warning(
                "No tracks could be resolved from CSV '%s'", self.playlist_name
            )
            return None

        return Playlist(
            self.playlist_name,
            self.config,
            self.primary_client,
            pending_tracks,
        )

    async def _resolve_row(
        self,
        row: ExportifyCsvRow,
        folder: str,
        primary_qualities: list[int],
        fallback_qualities: list[int],
        status: "PendingCsvPlaylist.Status",
        callback,
    ) -> PendingCsvTrack | None:
        """Search both services for *row* and build a :class:`PendingCsvTrack`.

        When ``self.repair_mode`` is ``True``:
        - Fetches up to :data:`_REPAIR_SEARCH_LIMIT` results per service (3x
          the normal window).
        - Uses :func:`_pick_best_candidate_repair` which applies fuzzy title
          matching as a fallback when exact title matching fails.
        """
        csv_cfg = self._csv_cfg()
        escalation_value = getattr(
            csv_cfg, "escalation_search_limit", _REPAIR_SEARCH_LIMIT
        )
        if isinstance(escalation_value, bool) or not isinstance(
            escalation_value, (int, float, str)
        ):
            escalation_limit = _REPAIR_SEARCH_LIMIT
        else:
            try:
                escalation_limit = int(escalation_value or _REPAIR_SEARCH_LIMIT)
            except (TypeError, ValueError):
                escalation_limit = _REPAIR_SEARCH_LIMIT
        search_limit = _REPAIR_SEARCH_LIMIT if self.repair_mode else _SEARCH_LIMIT
        primary_min_score = _provider_threshold(
            csv_cfg, self.primary_client.source, repair_mode=self.repair_mode
        )
        fallback_min_score = _provider_threshold(
            csv_cfg,
            self.fallback_client.source if self.fallback_client else "",
            repair_mode=self.repair_mode,
        )
        low_score_floor = max(
            0,
            int(
                self.lowscore_floor
                if self.lowscore_floor is not None
                else _DEFAULT_LOW_SCORE_FLOOR
            ),
        )
        ratio, seconds = self._get_duration_tolerance()

        primary_outcome = ResolverOutcome(None, "no results", "", "")
        fallback_outcome = ResolverOutcome(None, "no results", "", "")
        telemetry = self._telemetry()

        if not is_usable_exportify_row(row):
            reason = "invalid row: missing track name or artist"
            logger.warning("Skipping unresolved CSV row %d (%s)", row.row_index, reason)
            status.unresolved += 1
            if self.db.unresolved_log is not None:
                self.db.unresolved_log.log(
                    track_name=row.track_name,
                    artists=row.artists_raw,
                    album=row.album,
                    release_date=row.release_date,
                    isrc=row.isrc,
                    spotify_uri=row.spotify_uri,
                    primary_source=self.primary_client.source,
                    fallback_source=self.fallback_client.source
                    if self.fallback_client
                    else "",
                    reason=reason,
                    row_index=row.row_index,
                    source_row_index=row.source_row_index,
                    original_position=row.position,
                    duration_ms=row.duration_ms,
                    session_country=_session_country_hint(),
                )
            if callback:
                await callback()
            return None

        local_skip_hit, local_path, local_reason = self._try_local_skip(row)
        if local_skip_hit:
            status.local_skipped += 1
            self.db.set_skipped()
            logger.debug(
                "CSV local-skip (%s) matched path '%s' for row %d (%s - %s)",
                local_reason,
                local_path,
                row.row_index,
                row.track_name,
                row.artists_raw,
            )
            if callback:
                await callback()
            return None
        if local_reason in {
            "ambiguous-local-not-skipped",
            "duration-mismatch-local-not-skipped",
        }:
            logger.debug(
                "CSV local-skip did not skip row %d (%s): %s",
                row.row_index,
                row.track_name,
                local_reason,
            )
        match_policy = MatchPolicy.from_config(self._csv_cfg())

        async def _resolve_for_client(
            client: Client,
            *,
            escalation: bool = False,
        ) -> ResolverOutcome:
            """Discover broadly, then choose the strongest strictly accepted match."""
            queries = _build_search_queries(row, client.source, escalation=escalation)
            provider_min_score = _provider_threshold(
                csv_cfg, client.source, repair_mode=self.repair_mode
            )
            if (
                self.provider_budgets is not None
                and self.provider_budgets[client.source].disabled
            ):
                return ResolverOutcome(
                    candidate=None,
                    reason="provider disabled (auth/session failure)",
                    query="",
                    strategy="provider-disabled",
                )

            best_low_conf: tuple[str, str, TrackCandidate] | None = None
            closest_rejected: list[TrackCandidate] = []
            best_score_by_id: dict[str, int] = {}
            matched_hits: list[tuple[str, str, TrackCandidate]] = []
            attempts: list[dict[str, object]] = []
            had_error = False
            last_query = ""
            last_strategy = ""

            def _add_rejected(candidate: TrackCandidate, reason_code: str = "") -> None:
                reason_codes = candidate.reason_codes
                if reason_code and reason_code not in reason_codes:
                    reason_codes = tuple((*reason_codes, reason_code))
                enriched = TrackCandidate(
                    source=candidate.source,
                    id=candidate.id,
                    title=candidate.title,
                    artist=candidate.artist,
                    album=candidate.album,
                    release_date=candidate.release_date,
                    isrc=candidate.isrc,
                    score=candidate.score,
                    client=candidate.client,
                    reason_codes=reason_codes,
                    signals=candidate.signals,
                    confidence=candidate.confidence,
                    margin_to_second=candidate.margin_to_second,
                )
                for index, existing in enumerate(closest_rejected):
                    if (existing.source, existing.id) == (enriched.source, enriched.id):
                        if enriched.score > existing.score:
                            closest_rejected[index] = enriched
                        return
                closest_rejected.append(enriched)
                closest_rejected.sort(key=lambda c: c.score, reverse=True)
                del closest_rejected[4:]

            def _record_hit(
                query: str, strategy: str, candidate: TrackCandidate
            ) -> None:
                for index, (_, _, existing) in enumerate(matched_hits):
                    if (
                        existing.id == candidate.id
                        and existing.source == candidate.source
                    ):
                        if candidate.score > existing.score:
                            matched_hits[index] = (query, strategy, candidate)
                        return
                matched_hits.append((query, strategy, candidate))

            def _refresh_candidate_confidence(candidate: TrackCandidate) -> None:
                candidate.margin_to_second = _margin_to_second_best(
                    candidate.score, list(best_score_by_id.values())
                )
                candidate.confidence = _confidence_for_candidate(
                    candidate, provider_min_score, candidate.margin_to_second
                )

            hinted_id = ""
            if self.repair_mode and row.repair_candidate_ids:
                hinted_id = (row.repair_candidate_ids.get(client.source) or "").strip()

            if hinted_id and not escalation:
                try:
                    if (
                        self.provider_budgets is not None
                        and client.source in self.provider_budgets
                    ):
                        async with self.provider_budgets[client.source].metadata_sem:
                            await self._provider_wait(client.source)
                            hinted_resp = await client.get_metadata(hinted_id, "track")
                    else:
                        hinted_resp = await client.get_metadata(hinted_id, "track")
                    self._provider_after_call(client.source, ok=True, err=None)
                    explain = explain_candidate_score(
                        row,
                        _item_title(client.source, hinted_resp),
                        _item_artist(client.source, hinted_resp),
                        _item_album(client.source, hinted_resp),
                        _item_date(client.source, hinted_resp),
                        _item_isrc(client.source, hinted_resp),
                        _item_duration_ms(client.source, hinted_resp),
                        policy=match_policy,
                    )
                    hinted_score = (
                        score_candidate_repair(
                            row,
                            _item_title(client.source, hinted_resp),
                            _item_artist(client.source, hinted_resp),
                            _item_album(client.source, hinted_resp),
                            _item_date(client.source, hinted_resp),
                            _item_isrc(client.source, hinted_resp),
                            _item_duration_ms(client.source, hinted_resp),
                            policy=match_policy,
                        )
                        if self.repair_mode
                        else explain.score
                    )
                    hinted_candidate = TrackCandidate(
                        source=client.source,
                        id=str(hinted_resp.get("id", hinted_id)),
                        title=_item_title(client.source, hinted_resp),
                        artist=_item_artist(client.source, hinted_resp),
                        album=_item_album(client.source, hinted_resp),
                        release_date=_item_date(client.source, hinted_resp),
                        isrc=_item_isrc(client.source, hinted_resp),
                        score=hinted_score,
                        client=client,
                        reason_codes=explain.reason_codes,
                        signals=explain.signals,
                    )
                    best_score_by_id[hinted_candidate.id] = hinted_candidate.score
                    _refresh_candidate_confidence(hinted_candidate)
                    attempts.append(
                        {
                            "strategy": "id-hint",
                            "query": hinted_id,
                            "result_count": 1,
                            "shortlist_count": 1,
                            "error": "",
                        }
                    )
                    if hinted_candidate.score >= provider_min_score:
                        # A repair candidate ID is a deterministic provider-track
                        # identity captured by a previous resolver pass.  Once its
                        # current metadata still clears the strict scorer, honour
                        # it as the repair fast path instead of spending new search
                        # calls trying to beat an already validated ID.
                        return ResolverOutcome(
                            candidate=hinted_candidate,
                            reason="matched",
                            query=hinted_id,
                            strategy="id-hint",
                            rejected=closest_rejected,
                            attempts=tuple(attempts),
                        )
                    elif hinted_candidate.score > 0:
                        best_low_conf = (hinted_id, "id-hint", hinted_candidate)
                        _add_rejected(hinted_candidate, "reject_below_threshold")
                    else:
                        _add_rejected(hinted_candidate)
                except Exception as e:
                    self._provider_after_call(client.source, ok=False, err=e)
                    attempts.append(
                        {
                            "strategy": "id-hint",
                            "query": hinted_id,
                            "result_count": 0,
                            "shortlist_count": 0,
                            "error": type(e).__name__,
                        }
                    )
                    logger.debug(
                        "Hinted metadata lookup failed on %s id=%s: %s",
                        client.source,
                        hinted_id,
                        e,
                    )

            for strategy, query in queries:
                if (
                    self.provider_budgets is not None
                    and self.provider_budgets[client.source].disabled
                ):
                    break

                # Do not spend a broad title-only call when a previous strong or
                # alternate query already produced a clearly separated match.
                if strategy in _BROAD_SEARCH_STRATEGIES and matched_hits:
                    provisional = _select_best_candidate(matched_hits)
                    if provisional is not None:
                        _refresh_candidate_confidence(provisional[2])
                        if provisional[
                            2
                        ].score >= provider_min_score + 10 and provisional[
                            2
                        ].confidence in {CONF_MEDIUM, CONF_HIGH}:
                            break

                last_query = query
                last_strategy = strategy
                top_candidates: list[TrackCandidate] = []
                pages: list[dict] = []
                try:
                    effective_limit = (
                        escalation_limit
                        if escalation or strategy in _BROAD_SEARCH_STRATEGIES
                        else search_limit
                    )
                    cache_key = (client.source, query.casefold(), effective_limit)
                    if self.query_cache is not None and cache_key in self.query_cache:
                        pages = self.query_cache[cache_key]
                    else:
                        if self.provider_budgets is not None:
                            async with self.provider_budgets[client.source].search_sem:
                                await self._provider_wait(client.source)
                                pages = await client.search(
                                    "track", query, limit=effective_limit
                                )
                        else:
                            pages = await client.search(
                                "track", query, limit=effective_limit
                            )
                        self._provider_after_call(client.source, ok=True, err=None)
                        if self.query_cache is not None:
                            self.query_cache[cache_key] = pages

                    raw_items = _extract_raw_results(client.source, pages)
                    top_candidates = _pick_top_candidates(
                        row,
                        client.source,
                        pages,
                        client,
                        repair_mode=self.repair_mode,
                        limit=_SHORTLIST_K,
                        policy=match_policy,
                    )
                    attempts.append(
                        {
                            "strategy": strategy,
                            "query": query,
                            "result_count": len(raw_items),
                            "shortlist_count": len(top_candidates),
                            "error": "",
                        }
                    )
                    for rejected in _top_rejected_candidates(
                        row,
                        client.source,
                        pages,
                        client,
                        policy=match_policy,
                        limit=2,
                    ):
                        _add_rejected(rejected)

                    for cand in top_candidates:
                        best_score_by_id[cand.id] = max(
                            best_score_by_id.get(cand.id, 0), cand.score
                        )
                    for cand in top_candidates:
                        _refresh_candidate_confidence(cand)
                except Exception as e:
                    self._provider_after_call(client.source, ok=False, err=e)
                    attempts.append(
                        {
                            "strategy": strategy,
                            "query": query,
                            "result_count": 0,
                            "shortlist_count": 0,
                            "error": type(e).__name__,
                        }
                    )
                    if (
                        self.provider_budgets is not None
                        and self.provider_budgets[client.source].disabled
                    ):
                        break
                    had_error = True
                    logger.debug(
                        "Search failed on %s (%s) for '%s': %s",
                        client.source,
                        strategy,
                        query,
                        e,
                    )
                    continue

                for candidate in top_candidates:
                    reason = _candidate_reason(candidate, provider_min_score)
                    if reason == "matched":
                        _record_hit(query, strategy, candidate)
                        if (
                            candidate.score == 100
                            and "accepted_isrc_match" in candidate.reason_codes
                        ):
                            return ResolverOutcome(
                                candidate=candidate,
                                reason="matched",
                                query=query,
                                strategy=strategy,
                                rejected=closest_rejected[:2],
                                attempts=tuple(attempts),
                            )
                    else:
                        if (
                            best_low_conf is None
                            or candidate.score > best_low_conf[2].score
                        ):
                            best_low_conf = (query, strategy, candidate)
                        _add_rejected(candidate, "reject_below_threshold")

            selected_hit = _select_best_candidate(matched_hits)
            if selected_hit is not None:
                query, strategy, candidate = selected_hit
                _refresh_candidate_confidence(candidate)
                if (
                    strategy in _BROAD_SEARCH_STRATEGIES
                    and candidate.margin_to_second <= 2
                    and candidate.score < 95
                ):
                    return ResolverOutcome(
                        candidate=candidate,
                        reason=REASON_AMBIGUOUS,
                        query=query,
                        strategy=strategy,
                        rejected=closest_rejected[:2],
                        attempts=tuple(attempts),
                    )
                return ResolverOutcome(
                    candidate=candidate,
                    reason="matched",
                    query=query,
                    strategy=strategy,
                    rejected=closest_rejected[:2],
                    attempts=tuple(attempts),
                )

            if best_low_conf is not None:
                query, strategy, candidate = best_low_conf
                return ResolverOutcome(
                    candidate=candidate,
                    reason=_candidate_reason(candidate, provider_min_score),
                    query=query,
                    strategy=strategy,
                    rejected=closest_rejected[:2],
                    attempts=tuple(attempts),
                )

            if closest_rejected:
                candidate = closest_rejected[0]
                return ResolverOutcome(
                    candidate=candidate,
                    reason=_rejection_reason(candidate),
                    query=last_query,
                    strategy=last_strategy,
                    rejected=closest_rejected[:2],
                    attempts=tuple(attempts),
                )

            if had_error:
                return ResolverOutcome(
                    candidate=None,
                    reason=REASON_PROVIDER_SEARCH_ERROR,
                    query=last_query,
                    strategy=last_strategy,
                    attempts=tuple(attempts),
                )

            attempted_broad = any(
                str(attempt.get("strategy", "")) in _BROAD_SEARCH_STRATEGIES
                for attempt in attempts
            )
            return ResolverOutcome(
                candidate=None,
                reason=(
                    REASON_NO_RESULTS_AFTER_BROAD
                    if attempted_broad
                    else REASON_NO_RESULTS
                ),
                query=" || ".join(
                    str(a.get("query", "")) for a in attempts if a.get("query")
                ),
                strategy=" / ".join(
                    str(a.get("strategy", "")) for a in attempts if a.get("strategy")
                ),
                rejected=closest_rejected[:2],
                attempts=tuple(attempts),
            )

        primary_outcome = await _resolve_for_client(
            self.primary_client, escalation=False
        )

        def _eligible_for_escalation(outcome: ResolverOutcome) -> bool:
            return (
                outcome.reason == REASON_NO_RESULTS
                or outcome.reason == REASON_NO_RESULTS_AFTER_BROAD
                or outcome.reason.startswith(REASON_LOW_CONFIDENCE)
            )

        # Search fallback service if configured and primary did not strictly match.
        if self.fallback_client is not None and primary_outcome.reason != "matched":
            fallback_outcome = await _resolve_for_client(
                self.fallback_client, escalation=False
            )

        # Only repeat with the expanded result window for genuine discovery
        # misses. Provider errors and explicit safety rejections are terminal for
        # this row/provider and must not be amplified into repeated calls.
        if _eligible_for_escalation(primary_outcome):
            primary_outcome = await _resolve_for_client(
                self.primary_client, escalation=True
            )
        if (
            self.fallback_client is not None
            and fallback_outcome.reason != "matched"
            and _eligible_for_escalation(fallback_outcome)
            and primary_outcome.reason != "matched"
        ):
            fallback_outcome = await _resolve_for_client(
                self.fallback_client, escalation=True
            )

        primary_candidate = (
            primary_outcome.candidate
            if primary_outcome.reason == "matched"
            and primary_outcome.candidate
            and primary_outcome.candidate.score >= primary_min_score
            else None
        )
        fallback_candidate = (
            fallback_outcome.candidate
            if fallback_outcome.reason == "matched"
            and fallback_outcome.candidate
            and fallback_outcome.candidate.score >= fallback_min_score
            else None
        )

        async def _guarded_low_score_candidate(
            outcome: ResolverOutcome | None,
        ) -> TrackCandidate | None:
            if not self.repair_mode or not self.accept_lowscore or outcome is None:
                return None
            candidate = outcome.candidate
            if candidate is None:
                return None
            provider_min = (
                primary_min_score
                if candidate.source == self.primary_client.source
                else fallback_min_score
            )
            if candidate.score < low_score_floor or candidate.score >= provider_min:
                return None
            if not row.artists_list and not row.artists_raw:
                return None
            if not _artist_overlap(
                row.artists_list or [row.artists_raw], candidate.artist
            ):
                return None
            # Duration sanity: only enforced when expected exists.
            if row.duration_ms:
                try:
                    resp = await _budgeted_get_raw_metadata(
                        candidate,
                        self.provider_budgets,
                        self._provider_wait,
                        self._provider_after_call,
                    )
                    duration_ms = _item_duration_ms(candidate.source, resp)
                    actual_seconds = (duration_ms / 1000.0) if duration_ms else None
                except Exception as e:
                    logger.debug(
                        "Low-score duration guard read failed for %s:%s: %s",
                        candidate.source,
                        candidate.id,
                        e,
                    )
                    actual_seconds = None
                if not _duration_match_with_tolerance(
                    row.duration_ms,
                    actual_seconds,
                    tolerance_ratio=ratio,
                    tolerance_seconds=seconds,
                ):
                    return None
            return candidate

        # choose best guarded low-score candidate if strict acceptance failed
        if (
            primary_candidate is None
            and fallback_candidate is None
            and self.repair_mode
            and self.accept_lowscore
        ):
            gathered_results = await asyncio.gather(
                _guarded_low_score_candidate(primary_outcome),
                _guarded_low_score_candidate(fallback_outcome),
                return_exceptions=True,
            )
            low_candidates = [
                c for c in gathered_results if isinstance(c, TrackCandidate)
            ]
            if low_candidates:
                if (
                    len(low_candidates) > 1
                    and (low_candidates[0].source, low_candidates[0].id)
                    != (low_candidates[1].source, low_candidates[1].id)
                    and abs(low_candidates[0].score - low_candidates[1].score) <= 3
                ):
                    logger.warning(
                        "Repair low-score rejected '%s' due to ambiguous competing candidates: %s:%s(score=%d) vs %s:%s(score=%d)",
                        row.track_name,
                        low_candidates[0].source,
                        low_candidates[0].id,
                        low_candidates[0].score,
                        low_candidates[1].source,
                        low_candidates[1].id,
                        low_candidates[1].score,
                    )
                else:
                    picked = sorted(
                        low_candidates,
                        key=lambda c: (
                            c.score,
                            1 if c.source == self.primary_client.source else 0,
                        ),
                        reverse=True,
                    )[0]
                    if picked.source == self.primary_client.source:
                        primary_candidate = picked
                    else:
                        fallback_candidate = picked
                    status.low_score_accepted += 1
                    logger.warning(
                        "Repair low-score accepted '%s' via %s score=%d floor=%d",
                        row.track_name,
                        picked.source,
                        picked.score,
                        low_score_floor,
                    )

        selected = primary_candidate or fallback_candidate
        telemetry.log(
            {
                "schema_version": TELEMETRY_SCHEMA_VERSION,
                "row": {
                    "row_index": row.row_index,
                    "source_row_index": row.source_row_index,
                    "track_name": row.track_name,
                    "artists": row.artists_raw,
                    "album": row.album,
                    "release_date": row.release_date,
                    "isrc": row.isrc,
                },
                "provider_outcomes": {
                    "primary": {
                        "source": self.primary_client.source,
                        "threshold": primary_min_score,
                        "reason": primary_outcome.reason,
                        "candidate": _candidate_payload(primary_outcome.candidate),
                        "rejected": _rejected_candidate_payloads(
                            primary_outcome.rejected
                        ),
                        "attempts": list(primary_outcome.attempts),
                    },
                    "fallback": {
                        "source": (
                            self.fallback_client.source if self.fallback_client else ""
                        ),
                        "threshold": fallback_min_score,
                        "reason": fallback_outcome.reason,
                        "candidate": _candidate_payload(fallback_outcome.candidate),
                        "rejected": _rejected_candidate_payloads(
                            fallback_outcome.rejected
                        ),
                        "attempts": list(fallback_outcome.attempts),
                    },
                },
                "selected": _candidate_payload(selected),
            }
        )

        if primary_candidate is None and fallback_candidate is None:
            failure_reasons = ", ".join(
                [
                    f"{self.primary_client.source}: {primary_outcome.reason}",
                    (
                        f"{self.fallback_client.source}: {fallback_outcome.reason}"
                        if self.fallback_client
                        else ""
                    ),
                ]
            ).strip(", ")
            logger.warning(
                "Could not confidently resolve '%s' by %s (%s)",
                row.track_name,
                row.artists_raw,
                failure_reasons,
            )
            status.unresolved += 1
            if self.db.unresolved_log is not None:
                self.db.unresolved_log.log(
                    track_name=row.track_name,
                    artists=row.artists_raw,
                    album=row.album,
                    release_date=row.release_date,
                    isrc=row.isrc,
                    spotify_uri=row.spotify_uri,
                    primary_source=self.primary_client.source,
                    fallback_source=self.fallback_client.source
                    if self.fallback_client
                    else "",
                    reason=failure_reasons or "metadata mismatch",
                    row_index=row.row_index,
                    source_row_index=row.source_row_index,
                    original_position=row.position,
                    duration_ms=row.duration_ms,
                    primary_candidate_id=(
                        primary_outcome.candidate.id
                        if primary_outcome.candidate is not None
                        else ""
                    ),
                    fallback_candidate_id=(
                        fallback_outcome.candidate.id
                        if fallback_outcome.candidate is not None
                        else ""
                    ),
                    session_country=_session_country_hint(),
                    query_strategy=" / ".join(
                        s
                        for s in (primary_outcome.strategy, fallback_outcome.strategy)
                        if s
                    ),
                    attempted_query=" || ".join(
                        q for q in (primary_outcome.query, fallback_outcome.query) if q
                    ),
                    primary_confidence=(
                        primary_outcome.candidate.confidence
                        if primary_outcome.candidate is not None
                        else CONF_REJECT
                    ),
                    fallback_confidence=(
                        fallback_outcome.candidate.confidence
                        if fallback_outcome.candidate is not None
                        else CONF_REJECT
                    ),
                    primary_margin=(
                        str(primary_outcome.candidate.margin_to_second)
                        if primary_outcome.candidate is not None
                        else ""
                    ),
                    fallback_margin=(
                        str(fallback_outcome.candidate.margin_to_second)
                        if fallback_outcome.candidate is not None
                        else ""
                    ),
                    primary_reject_1=(
                        _serialize_rejected_candidate(
                            (primary_outcome.rejected or [None])[0]
                        )
                        if primary_outcome.rejected
                        else ""
                    ),
                    primary_reject_2=(
                        _serialize_rejected_candidate(
                            (primary_outcome.rejected or [None, None])[1]
                        )
                        if primary_outcome.rejected
                        and len(primary_outcome.rejected) > 1
                        else ""
                    ),
                    fallback_reject_1=(
                        _serialize_rejected_candidate(
                            (fallback_outcome.rejected or [None])[0]
                        )
                        if fallback_outcome.rejected
                        else ""
                    ),
                    fallback_reject_2=(
                        _serialize_rejected_candidate(
                            (fallback_outcome.rejected or [None, None])[1]
                        )
                        if fallback_outcome.rejected
                        and len(fallback_outcome.rejected) > 1
                        else ""
                    ),
                )
            if callback:
                await callback()
            return None

        status.found += 1
        if callback:
            await callback()

        return PendingCsvTrack(
            row=row,
            primary_candidate=primary_candidate,
            fallback_candidate=fallback_candidate,
            primary_qualities=primary_qualities,
            fallback_qualities=fallback_qualities,
            primary_source=self.primary_client.source,
            fallback_source=self.fallback_client.source if self.fallback_client else "",
            config=self.config,
            folder=folder,
            playlist_name=self.playlist_name,
            position=row.position,
            db=self.db,
            provider_budgets=self.provider_budgets,
            negative_candidate_cache=self.negative_candidate_cache,
        )


def _chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]
