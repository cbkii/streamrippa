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
from ..file_lists import ExportifyCsvRow, score_candidate
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

# Number of search results to fetch per service per row
_SEARCH_LIMIT = 5


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
        return (
            item.get("release_date")
            or item.get("album", {}).get("release_date", "")
        )
    if source == "qobuz":
        return (
            item.get("release_date_original")
            or item.get("album", {}).get("release_date_original", "")
        )
    if source == "tidal":
        return item.get("streamStartDate", "") or item.get("album", {}).get(
            "releaseDate", ""
        )
    return ""


def _item_isrc(source: str, item: dict) -> str:
    return (item.get("isrc") or "").strip()


def _pick_best_candidate(
    row: ExportifyCsvRow,
    source: str,
    pages: list[dict],
    client: Client,
) -> TrackCandidate | None:
    """Score all results from *pages* and return the best :class:`TrackCandidate`.

    Returns ``None`` if *pages* is empty or all items score 0 and the first
    result is also unavailable.
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

    if best_item is None:
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
            seen: dict[str, None] = {}
            merged: list[str] = []
            for g in provider_genres + csv_genres:
                lower_g = g.lower()
                if lower_g not in seen:
                    seen[lower_g] = None
                    merged.append(g)
            if merged:
                extra[target_tag] = ", ".join(merged)
        else:
            extra[target_tag] = value

    return extra if extra else None


@dataclass(slots=True)
class PendingCsvTrack(Pending):
    """A track derived from an Exportify CSV row, ready to resolve and download.

    Holds pre-searched candidates from both the primary and fallback services
    and implements the service-first / quality-second fallback algorithm.
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
        """
        max_passes = max(
            len(self.primary_qualities) if self.primary_candidate else 0,
            len(self.fallback_qualities) if self.fallback_candidate else 0,
        )

        for pass_idx in range(max_passes):
            # --- Try primary service at this pass's quality ---
            if self.primary_candidate and pass_idx < len(self.primary_qualities):
                track = await self._try_candidate(
                    self.primary_candidate,
                    self.primary_qualities[pass_idx],
                )
                if track is not None:
                    return track

            # --- Try fallback service at this pass's quality ---
            if self.fallback_candidate and pass_idx < len(self.fallback_qualities):
                track = await self._try_candidate(
                    self.fallback_candidate,
                    self.fallback_qualities[pass_idx],
                )
                if track is not None:
                    return track

        # All passes exhausted
        reason = "no candidate found" if (
            self.primary_candidate is None and self.fallback_candidate is None
        ) else "all quality/service combinations failed"

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
                reason=reason,
                row_index=self.row.row_index,
            )

        return None

    async def _try_candidate(
        self,
        candidate: TrackCandidate,
        quality: int,
    ) -> Track | None:
        """Attempt to resolve and prepare *candidate* at *quality*.

        Returns a :class:`Track` on success, ``None`` on any failure.
        The Deezer client is called with ``exact_quality=True`` so that it
        raises :class:`NonStreamableError` instead of silently stepping down.
        """
        # Source-aware duplicate check
        if self.db.downloaded(candidate.id, source=candidate.source):
            logger.info(
                "Track %s:%s already in database. Skipping.",
                candidate.source,
                candidate.id,
            )
            self.db.set_skipped()
            return None

        try:
            resp = await candidate.client.get_metadata(candidate.id, "track")
        except NonStreamableError as e:
            logger.debug(
                "Could not fetch metadata for %s:%s at quality %d: %s",
                candidate.source,
                candidate.id,
                quality,
                e,
            )
            return None
        except Exception as e:
            logger.debug(
                "Unexpected error fetching metadata for %s:%s: %s",
                candidate.source,
                candidate.id,
                e,
            )
            return None

        album = AlbumMetadata.from_track_resp(resp, candidate.source)
        if album is None:
            logger.debug(
                "Track %s:%s not available on %s",
                candidate.source,
                candidate.id,
                candidate.source,
            )
            return None

        meta = TrackMetadata.from_resp(album, candidate.source, resp)
        if meta is None:
            logger.debug(
                "Could not build TrackMetadata for %s:%s",
                candidate.source,
                candidate.id,
            )
            return None

        c = self.config.session.metadata
        if c.renumber_playlist_tracks:
            meta.tracknumber = self.position
        if c.set_playlist_to_album:
            album.album = self.playlist_name

        # Build extra tags from CSV row
        tag_map = c.exportify_tag_map if hasattr(c, "exportify_tag_map") else {}
        if tag_map:
            try:
                provider_genre = album.get_genres() if album.genre else None
                meta.extra_tags = _build_extra_tags(self.row, provider_genre, dict(tag_map))
            except Exception as e:
                logger.warning("Failed to build extra tags for '%s': %s", meta.title, e)

        # Attempt download at the requested quality.
        # Pass exact_quality=True for Deezer so the caller controls stepping.
        is_deezer = candidate.source == "deezer"
        try:
            if is_deezer:
                embedded_cover_path, downloadable = await asyncio.gather(
                    self._download_cover(album.covers, candidate.client),
                    candidate.client.get_downloadable(
                        candidate.id, quality, exact_quality=True
                    ),
                )
            else:
                embedded_cover_path, downloadable = await asyncio.gather(
                    self._download_cover(album.covers, candidate.client),
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
            return None
        except Exception as e:
            logger.debug(
                "Error getting downloadable for %s:%s: %s",
                candidate.source,
                candidate.id,
                e,
            )
            return None

        return Track(
            meta,
            downloadable,
            self.config,
            self.folder,
            embedded_cover_path,
            self.db,
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
    """

    playlist_name: str
    rows: list[ExportifyCsvRow]
    primary_client: Client
    fallback_client: Client | None
    config: Config
    db: Database

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
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error("Batch resolver error: %s", result)
                            status.failed += 1
                        elif result is not None:
                            pending_tracks.append(result)
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
                for result in results:
                    if isinstance(result, Exception):
                        logger.error("Batch resolver error: %s", result)
                        status.failed += 1
                    elif result is not None:
                        pending_tracks.append(result)

        logger.info(
            "CSV resolve complete: %d found, %d failed, %d unresolved out of %d rows",
            status.found,
            status.failed,
            status.unresolved,
            status.total,
        )

        if not pending_tracks:
            logger.warning("No tracks could be resolved from CSV '%s'", self.playlist_name)
            return None

        return Playlist(
            self.playlist_name,
            self.config,
            self.primary_client,
            pending_tracks,  # type: ignore[arg-type]
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
        """Search both services for *row* and build a :class:`PendingCsvTrack`."""
        query = f"{row.track_name} {row.artists_list[0] if row.artists_list else row.artists_raw}"

        primary_candidate: TrackCandidate | None = None
        fallback_candidate: TrackCandidate | None = None

        # Search primary service
        try:
            primary_pages = await self.primary_client.search(
                "track", query, limit=_SEARCH_LIMIT
            )
            primary_candidate = _pick_best_candidate(
                row, self.primary_client.source, primary_pages, self.primary_client
            )
        except Exception as e:
            logger.debug(
                "Search failed on %s for '%s': %s",
                self.primary_client.source,
                query,
                e,
            )

        # Search fallback service if configured
        if self.fallback_client is not None:
            try:
                fallback_pages = await self.fallback_client.search(
                    "track", query, limit=_SEARCH_LIMIT
                )
                fallback_candidate = _pick_best_candidate(
                    row,
                    self.fallback_client.source,
                    fallback_pages,
                    self.fallback_client,
                )
            except Exception as e:
                logger.debug(
                    "Search failed on %s for '%s': %s",
                    self.fallback_client.source,
                    query,
                    e,
                )

        if primary_candidate is None and fallback_candidate is None:
            logger.warning(
                "No results found for '%s' by %s",
                row.track_name,
                row.artists_raw,
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
                    fallback_source=self.fallback_client.source if self.fallback_client else "",
                    reason="no search results from any service",
                    row_index=row.row_index,
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
