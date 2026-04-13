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
import logging
import os
from dataclasses import dataclass

from rich.text import Text

from ..client import Client
from ..config import Config
from ..console import console
from ..db import Database
from ..exceptions import NonStreamableError
from ..file_lists import ExportifyCsvRow, score_candidate, score_candidate_repair
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
_SEARCH_LIMIT = 5

# Expanded search window used in repair mode for heavier matching
_REPAIR_SEARCH_LIMIT = 15
_MIN_ACCEPTABLE_SCORE = 50
_MIN_ACCEPTABLE_SCORE_REPAIR = 30


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


@dataclass(slots=True)
class ResolverOutcome:
    candidate: TrackCandidate | None
    reason: str
    query: str
    strategy: str


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
    if source == "deezer":
        return item.get("release_date") or item.get("album", {}).get("release_date", "")
    if source == "qobuz":
        return item.get("release_date_original") or item.get("album", {}).get(
            "release_date_original", ""
        )
    if source == "tidal":
        return item.get("streamStartDate", "") or item.get("album", {}).get(
            "releaseDate", ""
        )
    return ""


def _item_isrc(source: str, item: dict) -> str:
    return (item.get("isrc") or "").strip()


def _build_search_queries(row: ExportifyCsvRow, source: str) -> list[tuple[str, str]]:
    """Build deterministic layered queries for Exportify row resolution.

    Returns ``[(strategy, query), ...]`` in priority order:
    1) ISRC-led (when available) for Deezer/Qobuz.
    2) Structured title + multiple artists + album/year hints.
    3) Generic fallback (legacy behaviour shape).
    """
    artist_joined = (
        " ".join(row.artists_list[:3]) if row.artists_list else row.artists_raw
    )
    year = row.release_date[:4].strip() if row.release_date else ""

    queries: list[tuple[str, str]] = []
    if row.isrc and source in {"deezer", "qobuz"}:
        queries.append(("isrc", row.isrc))

    structured_parts = [row.track_name, artist_joined, row.album, year]
    structured = " ".join(p for p in structured_parts if p).strip()
    if structured:
        queries.append(("structured", structured))

    generic = f"{row.track_name} {row.artists_list[0] if row.artists_list else row.artists_raw}".strip()
    if generic:
        queries.append(("generic", generic))

    # De-dupe while preserving deterministic order.
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for strategy, query in queries:
        qn = " ".join(query.split())
        key = f"{strategy}:{qn}"
        if qn and key not in seen:
            seen.add(key)
            out.append((strategy, qn))
    return out


def _pick_best_candidate(
    row: ExportifyCsvRow,
    source: str,
    pages: list[dict],
    client: Client,
) -> TrackCandidate | None:
    """Score all results from *pages* and return the best :class:`TrackCandidate`.

    Returns ``None`` if *pages* is empty.
    """
    items = _extract_raw_results(source, pages)
    if not items:
        return None

    best_item = None
    best_score = -1

    for item in items:
        title = _item_title(source, item)
        artist = _item_artist(source, item)
        album = _item_album(source, item)
        date = _item_date(source, item)
        isrc = _item_isrc(source, item)

        sc = score_candidate(row, title, artist, album, date, isrc)
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
) -> TrackCandidate | None:
    """Repair-mode variant of :func:`_pick_best_candidate`.

    Uses :func:`~streamrip.file_lists.score_candidate_repair` which includes
    fuzzy title matching as a fallback when exact title matching fails.  Only
    used from :class:`PendingCsvPlaylist` when ``repair_mode=True``.
    """
    items = _extract_raw_results(source, pages)
    if not items:
        return None

    best_item = None
    best_score = -1

    for item in items:
        title = _item_title(source, item)
        artist = _item_artist(source, item)
        album = _item_album(source, item)
        date = _item_date(source, item)
        isrc = _item_isrc(source, item)

        sc = score_candidate_repair(row, title, artist, album, date, isrc)
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
        return "no results"
    if candidate.score < min_score:
        return f"low confidence ({candidate.score}<{min_score})"
    return "matched"


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
        if statuses == {"matched unavailable"}:
            return "matched item found, but unavailable on current service"
        if "download failed" in statuses:
            return "download attempt failed after a valid service/quality match"
        if "quality unavailable" in statuses:
            return "matched item available, but requested/highest quality unavailable"
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

        try:
            resp = await candidate.client.get_metadata(candidate.id, "track")
        except NonStreamableError as e:
            logger.debug(
                "Could not fetch metadata for %s:%s: %s",
                candidate.source,
                candidate.id,
                e,
            )
            return _MetaFetchResult(status="provider-error")
        except Exception as e:
            logger.debug(
                "Unexpected error fetching metadata for %s:%s: %s",
                candidate.source,
                candidate.id,
                e,
            )
            return _MetaFetchResult(status="provider-error")

        album = AlbumMetadata.from_track_resp(resp, candidate.source)
        if album is None:
            logger.debug(
                "Track %s:%s not available on %s",
                candidate.source,
                candidate.id,
                candidate.source,
            )
            return _MetaFetchResult(status="matched unavailable")

        meta = TrackMetadata.from_resp(album, candidate.source, resp)
        if meta is None:
            logger.debug(
                "Could not build TrackMetadata for %s:%s",
                candidate.source,
                candidate.id,
            )
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
        is_deezer = candidate.source == "deezer"
        try:
            if is_deezer:
                embedded_cover_path, downloadable = await asyncio.gather(
                    self._download_cover(cached.album.covers, candidate.client),
                    candidate.client.get_downloadable(
                        candidate.id, quality, exact_quality=True
                    ),
                )
            else:
                embedded_cover_path, downloadable = await asyncio.gather(
                    self._download_cover(cached.album.covers, candidate.client),
                    candidate.client.get_downloadable(candidate.id, quality),
                )
        except NonStreamableError as e:
            logger.debug(
                "Quality %d not available for %s:%s: %s",
                quality,
                candidate.source,
                candidate.id,
                e,
            )
            return None, "quality unavailable"
        except Exception as e:
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
    items that implement the service-first / quality-second fallback.

    When ``repair_mode=True`` the resolver uses an expanded search window
    (:data:`_REPAIR_SEARCH_LIMIT`) and fuzzy title scoring
    (:func:`_pick_best_candidate_repair`) to recover rows that the lightweight
    main-path scorer left unresolved.
    """

    playlist_name: str
    rows: list[ExportifyCsvRow]
    primary_client: Client
    fallback_client: Client | None
    config: Config
    db: Database
    repair_mode: bool = False

    @dataclass(slots=True)
    class Status:
        found: int
        failed: int
        unresolved: int
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
                (f"{self.total} total", "bold"),
                ")",
            )

    async def resolve(self) -> Playlist | None:
        parent = self.config.session.downloads.folder
        folder = os.path.join(parent, clean_filepath(self.playlist_name))

        status = self.Status(0, 0, 0, len(self.rows))
        fail_fast = self.config.session.reliability.fail_fast

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
                        logger.warning(
                            "fail_fast: stopping CSV resolver after batch error."
                        )
                        break
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
                    logger.warning(
                        "fail_fast: stopping CSV resolver after batch error."
                    )
                    break

        logger.info(
            "CSV resolve complete: %d found, %d failed, %d unresolved out of %d rows",
            status.found,
            status.failed,
            status.unresolved,
            status.total,
        )

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
        search_limit = _REPAIR_SEARCH_LIMIT if self.repair_mode else _SEARCH_LIMIT
        picker = (
            _pick_best_candidate_repair if self.repair_mode else _pick_best_candidate
        )
        min_score = (
            _MIN_ACCEPTABLE_SCORE_REPAIR if self.repair_mode else _MIN_ACCEPTABLE_SCORE
        )

        primary_outcome = ResolverOutcome(None, "no results", "", "")
        fallback_outcome = ResolverOutcome(None, "no results", "", "")

        async def _resolve_for_client(client: Client) -> ResolverOutcome:
            """
            Resolve the best TrackCandidate for a single client by optionally using an ID hint and then running layered search queries.

            Attempts an optional per-service ID hint (when repair mode and a hint exist); if that yields an acceptable match (score >= min_score) it is returned immediately. Otherwise runs the deterministic layered queries in order, scoring results with the configured picker and returning the first candidate that meets the minimum score. If no candidate meets the threshold the highest-scoring low-confidence candidate seen is returned. If any search invocation raised an exception and no candidate was selected, the outcome reason is `"search_failed"`. If no results were found and no errors occurred, the outcome reason is `"no results"`.

            Returns:
                ResolverOutcome: Outcome with:
                  - candidate: the selected TrackCandidate or `None` if none found,
                  - reason: one of `"matched"`, `"low confidence (score<min_score)"`, `"search_failed"`, or `"no results"`,
                  - query: the query string that produced the returned candidate (or the last attempted query on failure),
                  - strategy: the query strategy that produced the returned candidate (or the last attempted strategy on failure).
            """
            queries = _build_search_queries(row, client.source)
            best_low_conf: tuple[str, str, TrackCandidate] | None = None
            had_error = False
            last_query = ""
            last_strategy = ""

            hinted_id = ""
            if self.repair_mode and row.repair_candidate_ids:
                hinted_id = (row.repair_candidate_ids.get(client.source) or "").strip()

            if hinted_id:
                try:
                    hinted_resp = await client.get_metadata(hinted_id, "track")
                    hinted_candidate = TrackCandidate(
                        source=client.source,
                        id=str(hinted_resp.get("id", hinted_id)),
                        title=_item_title(client.source, hinted_resp),
                        artist=_item_artist(client.source, hinted_resp),
                        album=_item_album(client.source, hinted_resp),
                        release_date=_item_date(client.source, hinted_resp),
                        isrc=_item_isrc(client.source, hinted_resp),
                        score=(
                            score_candidate_repair(
                                row,
                                _item_title(client.source, hinted_resp),
                                _item_artist(client.source, hinted_resp),
                                _item_album(client.source, hinted_resp),
                                _item_date(client.source, hinted_resp),
                                _item_isrc(client.source, hinted_resp),
                            )
                            if self.repair_mode
                            else score_candidate(
                                row,
                                _item_title(client.source, hinted_resp),
                                _item_artist(client.source, hinted_resp),
                                _item_album(client.source, hinted_resp),
                                _item_date(client.source, hinted_resp),
                                _item_isrc(client.source, hinted_resp),
                            )
                        ),
                        client=client,
                    )
                    reason = _candidate_reason(hinted_candidate, min_score)
                    if reason == "matched":
                        return ResolverOutcome(
                            candidate=hinted_candidate,
                            reason=reason,
                            query=hinted_id,
                            strategy="id-hint",
                        )
                    best_low_conf = (hinted_id, "id-hint", hinted_candidate)
                except Exception as e:
                    logger.debug(
                        "Hinted metadata lookup failed on %s id=%s: %s",
                        client.source,
                        hinted_id,
                        e,
                    )

            for strategy, query in queries:
                last_query = query
                last_strategy = strategy
                try:
                    pages = await client.search("track", query, limit=search_limit)
                    candidate = picker(row, client.source, pages, client)
                except Exception as e:
                    had_error = True
                    logger.debug(
                        "Search failed on %s (%s) for '%s': %s",
                        client.source,
                        strategy,
                        query,
                        e,
                    )
                    continue

                reason = _candidate_reason(candidate, min_score)
                if reason == "matched":
                    return ResolverOutcome(candidate, reason, query, strategy)
                if candidate is not None:
                    if (
                        best_low_conf is None
                        or candidate.score > best_low_conf[2].score
                    ):
                        best_low_conf = (query, strategy, candidate)

            if best_low_conf is not None:
                query, strategy, candidate = best_low_conf
                return ResolverOutcome(
                    candidate=candidate,
                    reason=_candidate_reason(candidate, min_score),
                    query=query,
                    strategy=strategy,
                )
            if had_error:
                return ResolverOutcome(
                    candidate=None,
                    reason="search_failed",
                    query=last_query,
                    strategy=last_strategy,
                )
            return ResolverOutcome(
                candidate=None,
                reason="no results",
                query=queries[-1][1] if queries else "",
                strategy=queries[-1][0] if queries else "",
            )

        primary_outcome = await _resolve_for_client(self.primary_client)

        # Search fallback service if configured
        if self.fallback_client is not None:
            fallback_outcome = await _resolve_for_client(self.fallback_client)

        primary_candidate = (
            primary_outcome.candidate
            if primary_outcome.candidate
            and primary_outcome.candidate.score >= min_score
            else None
        )
        fallback_candidate = (
            fallback_outcome.candidate
            if fallback_outcome.candidate
            and fallback_outcome.candidate.score >= min_score
            else None
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
        )


def _chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]
