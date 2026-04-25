"""File list parsing utilities for the ``rip file`` command.

Supports three modes:

- json: list of ``{"source", "media_type", "id"}`` objects
- exportify-csv: Exportify-format CSV export from Spotify
- urls: whitespace-separated service URLs (default/fallback)
"""

from __future__ import annotations

import csv
import json
import re
import warnings
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

# Columns that must be present in the first row to identify an Exportify CSV.
# Other columns are optional and ignored when missing.
_EXPORTIFY_DETECT_HEADERS: frozenset[str] = frozenset(
    {"Track URI", "Track Name", "Artist Name(s)"}
)

FileMode = Literal["json", "exportify-csv", "urls"]


@dataclass(slots=True)
class ExportifyCsvRow:
    """One row parsed from an Exportify CSV export."""

    track_name: str
    artists_raw: str
    # Split from ``artists_raw`` on ``;``
    artists_list: list[str]
    album: str
    release_date: str
    # ISRC tag if present (``ISRC`` or ``Track ISRC`` column)
    isrc: str
    # ``Track URI`` column value (``spotify:track:…``)
    spotify_uri: str
    # Optional columns for metadata mapping
    genres: str
    loudness: str
    tempo: str
    # 1-based position (from ``Position`` column or row order)
    position: int
    # 0-based index of this row in the file (for deterministic logging)
    row_index: int
    # Immutable 0-based index from the original source CSV order
    source_row_index: int = 0
    # Canonical/stripped title used for query fallback and scoring
    canonical_track_name: str = ""
    # Exportify "Duration (ms)" column when present
    duration_ms: int | None = None
    # Optional provider-ID hints from unresolved CSV logs: {source: track_id}
    repair_candidate_ids: dict[str, str] | None = None


def _artist_sort_key(value: str) -> str:
    """Normalize artist text for deterministic sorting/grouping."""
    value = value.casefold().strip()
    return re.sub(r"\s+", " ", value)


def exportify_artist_group_key(row: ExportifyCsvRow) -> str:
    """Build a deterministic artist grouping key for batching/sorting."""
    if row.artists_list:
        return _artist_sort_key(row.artists_list[0])
    return _artist_sort_key(row.artists_raw)


def backup_and_sort_exportify_csv(path: str) -> str:
    """Deprecated no-op.

    CSV imports now parse rows in-place and apply artist-aware batching in memory;
    no ``.original.csv`` backup file is created.
    """
    warnings.warn(
        "backup_and_sort_exportify_csv is deprecated; CSV sorting/partitioning is "
        "handled in-memory and no backup file is written.",
        DeprecationWarning,
        stacklevel=2,
    )
    return str(Path(path))


def parse_exportify_csv(path: str) -> tuple[str, list[ExportifyCsvRow]]:
    """Parse an Exportify CSV file.

    Opens with ``utf-8-sig`` encoding so that a leading BOM is handled
    transparently.  Unknown columns are ignored.

    Args:
        path: Path to the CSV file.

    Returns:
        A ``(playlist_name, rows)`` tuple where *playlist_name* is derived
        from the CSV filename stem.
    """
    playlist_name = Path(path).stem
    rows: list[ExportifyCsvRow] = []

    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader):
            artists_raw = (row.get("Artist Name(s)") or "").strip()
            artists_list = [a.strip() for a in artists_raw.split(";") if a.strip()]

            try:
                position = int(row.get("Position") or i + 1)
            except (ValueError, TypeError):
                position = i + 1

            isrc = (row.get("ISRC") or row.get("Track ISRC") or "").strip()
            duration_ms: int | None = None
            try:
                duration_raw = (row.get("Duration (ms)") or "").strip()
                if duration_raw:
                    parsed_duration = int(duration_raw)
                    if parsed_duration > 0:
                        duration_ms = parsed_duration
            except (ValueError, TypeError):
                duration_ms = None
            track_name = (row.get("Track Name") or "").strip()

            rows.append(
                ExportifyCsvRow(
                    track_name=track_name,
                    artists_raw=artists_raw,
                    artists_list=artists_list,
                    album=(row.get("Album Name") or "").strip(),
                    release_date=(row.get("Release Date") or "").strip(),
                    isrc=isrc,
                    spotify_uri=(row.get("Track URI") or "").strip(),
                    genres=(row.get("Genres") or "").strip(),
                    loudness=(row.get("Loudness") or "").strip(),
                    tempo=(row.get("Tempo") or "").strip(),
                    position=position,
                    row_index=i,
                    source_row_index=i,
                    canonical_track_name=strip_title_decorators(track_name),
                    duration_ms=duration_ms,
                )
            )

    return playlist_name, rows


def partition_exportify_rows_artist_batched(
    rows: list[ExportifyCsvRow],
    max_batch_size: int = 40,
) -> list[list[ExportifyCsvRow]]:
    """Partition rows into bounded batches, keeping artist groups intact."""
    if max_batch_size < 1:
        raise ValueError("max_batch_size must be >= 1")

    groups: dict[str, list[ExportifyCsvRow]] = defaultdict(list)
    for row in rows:
        key = exportify_artist_group_key(row)
        if row.album:
            key = f"{key}::{_normalise_variant_text(row.album)}"
        groups[key].append(row)

    ordered_group_keys = sorted(groups.keys())
    batches: list[list[ExportifyCsvRow]] = []
    current: list[ExportifyCsvRow] = []
    for key in ordered_group_keys:
        group = sorted(groups[key], key=lambda r: (r.source_row_index, r.row_index))
        current.extend(group)
        if len(current) >= max_batch_size:
            batches.append(current)
            current = []

    if current:
        batches.append(current)

    return batches


def detect_file_mode(content: str) -> FileMode:
    """Detect the file mode from the text content of a file.

    Priority:
    1. JSON list (``[{…}, …]``)
    2. Exportify CSV (header row contains the required columns)
    3. URL list (fallback)

    The function strips a leading UTF-8 BOM before testing.
    """
    # Strip BOM if present
    content = content.lstrip("\ufeff")
    stripped = content.strip()

    # 1. Try JSON
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return "json"
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Try Exportify CSV header detection
    first_line = stripped.split("\n")[0] if stripped else ""
    try:
        headers = next(csv.reader([first_line]))
        if _EXPORTIFY_DETECT_HEADERS.issubset(set(headers)):
            return "exportify-csv"
    except StopIteration:
        pass

    # 3. Fallback: URL list
    return "urls"


def _normalise(s: str) -> str:
    """Lightweight text normalisation used for candidate scoring.

    - lowercase
    - strip leading/trailing whitespace
    - collapse runs of whitespace/hyphens/underscores to a single space
    - remove most punctuation (keep word chars and spaces)
    """
    s = s.lower().strip()
    s = re.sub(r"[\s\-_]+", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    return s


def strip_title_decorators(title: str) -> str:
    """Strip common title decorations while preserving core song identity.

    Conservative behavior:
    - Remove bracketed/parenthesized featured-artist segments.
    - Remove common trailing variant suffixes (e.g. ``- 2011 Remaster``).
    """
    if not title:
        return ""

    stripped = title
    # Drop "(feat. ...)" / "[featuring ...]" blocks entirely.
    stripped = re.sub(
        r"\s*[\(\[]\s*(?:feat(?:uring)?|ft)\.?\s+[^\)\]]*[\)\]]\s*",
        " ",
        stripped,
        flags=re.IGNORECASE,
    )

    # Drop trailing "- Remaster/Live/Edit/Version/Mono/Stereo/Deluxe..." suffixes.
    stripped = re.sub(
        r"\s*[-\u2013—:]\s*(?:\d{4}\s+)?(?:remaster(?:ed)?|live|edit|version|mono|stereo|deluxe|explicit|clean)\b.*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(
        r"\s*[-\u2013—:]\s*(?:acoustic|live|session|mix|version|edit|remaster(?:ed)?|unplugged)\b.*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    )

    return re.sub(r"\s+", " ", stripped).strip()


def _artist_overlap(query_artists: list[str], result_artist: str) -> bool:
    """Return True if any query artist name substantially overlaps with the
    result's artist string."""
    norm_result = _normalise(result_artist)
    for artist in query_artists:
        na = _normalise(artist)
        if na and (na in norm_result or norm_result in na):
            return True
    return False


_VARIANT_MARKERS: frozenset[str] = frozenset(
    {
        "live",
        "remaster",
        "remastered",
        "deluxe",
        "radio edit",
        "edit",
        "explicit",
        "clean",
        "mono",
        "stereo",
        "version",
        "bonus track",
        "feat",
        "featuring",
        "ft",
    }
)


def _normalise_variant_text(s: str) -> str:
    """
    Normalize a track/album title and remove common edition/version markers and standalone year tokens.

    Performs the same normalization as _normalise, then strips known variant markers (e.g. "live", "remaster", "feat") when they appear as standalone words and removes standalone four-digit years starting with 19 or 20. Collapses repeated whitespace and returns an empty string if the resulting text is empty.

    Parameters:
        s (str): Input text to normalise.

    Returns:
        str: Normalised text with variant markers and standalone year tokens removed.
    """
    norm = _normalise(strip_title_decorators(s))
    if not norm:
        return ""
    # Remove known variant markers and standalone year tags.
    marker_pattern = "|".join(
        sorted(
            (re.escape(marker) for marker in _VARIANT_MARKERS), key=len, reverse=True
        )
    )
    norm = re.sub(rf"\b(?:{marker_pattern})\b", "", norm)
    norm = re.sub(r"\b(?:19|20)\d{2}\b", "", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm


def _artist_coverage(query_artists: list[str], result_artist: str) -> float:
    """Compute deterministic overlap ratio for multi-artist rows.

    Returns:
        Value in [0.0, 1.0] where 1.0 means every query artist was observed in
        the candidate artist string.
    """
    if not query_artists:
        return 0.0
    norm_result = _normalise(result_artist)
    if not norm_result:
        return 0.0

    matched = 0
    for artist in query_artists:
        na = _normalise(artist)
        if na and (na in norm_result or norm_result in na):
            matched += 1
    return matched / len(query_artists)


def _year_bonus(row_date: str, candidate_date: str) -> int:
    if not row_date or not candidate_date:
        return 0
    try:
        row_year = int(str(row_date)[:4])
        cand_year = int(str(candidate_date)[:4])
    except (TypeError, ValueError):
        return 0
    diff = abs(row_year - cand_year)
    if diff == 0:
        return 6
    if diff == 1:
        return 3
    if diff == 2:
        return 1
    return 0


def _variant_penalty(row_title: str, candidate_title: str) -> int:
    row_norm = _normalise(row_title)
    cand_norm = _normalise(candidate_title)
    if not row_norm or not cand_norm:
        return 0

    penalty = 0
    for marker in _VARIANT_MARKERS:
        in_row = marker in row_norm
        in_cand = marker in cand_norm
        if in_row != in_cand:
            penalty += 4
    return min(penalty, 16)


def score_candidate(
    row: "ExportifyCsvRow",
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_date: str,
    candidate_isrc: str,
    candidate_duration_ms: int | None = None,
) -> int:
    """
    Score how well a search-result candidate matches a CSV row from an Exportify export.

    Uses deterministic heuristics combining ISRC, title, artist, album and release year:
    - Exact ISRC match yields 100.
    - Requires either exact normalised title match or exact normalised-variant title match to produce a non-zero score.
    - Base score for title match is 42, with bonuses for exact normalised title, artist coverage, album match, and year; penalties for variant/edition mismatches.
    - Minimum positive score for matching titles is 1.

    Parameters:
        row (ExportifyCsvRow): CSV row providing `track_name`, `artists_list`, `album`, `release_date`, and optional `isrc`.
        candidate_title (str): Candidate track title to compare.
        candidate_artist (str): Candidate artist string to compare against `row.artists_list`.
        candidate_album (str): Candidate album string to compare.
        candidate_date (str): Candidate release date string to compare for year bonus.
        candidate_isrc (str): Candidate ISRC code for exact-match short-circuit.

    Returns:
        int: Numeric match score. `100` indicates exact ISRC match; `0` indicates no title match; otherwise a positive score (at least `1`) representing match strength.
    """
    # ISRC match is definitive
    if row.isrc and candidate_isrc:
        if row.isrc.upper() == candidate_isrc.upper():
            return 100

    norm_title = _normalise(row.track_name)
    norm_cand = _normalise(candidate_title)
    if not norm_title or not norm_cand:
        return 0

    # Strong title requirement first; this keeps generic text search as fallback
    # but prevents unrelated tracks from winning on weak metadata overlap.
    titles_match = norm_title == norm_cand
    row_variant_title = _normalise_variant_text(row.track_name)
    candidate_variant_title = _normalise_variant_text(candidate_title)
    variant_titles_match = (
        bool(row_variant_title)
        and bool(candidate_variant_title)
        and row_variant_title == candidate_variant_title
    )

    if not titles_match and not variant_titles_match:
        return 0

    score = 42
    if titles_match:
        score += 8

    coverage = _artist_coverage(row.artists_list, candidate_artist)
    if coverage >= 1.0:
        score += 24
    elif coverage >= 0.5:
        score += 14
    elif _artist_overlap(row.artists_list, candidate_artist):
        score += 8

    if row.album and candidate_album:
        row_album = _normalise_variant_text(row.album)
        cand_album = _normalise_variant_text(candidate_album)
        if row_album and cand_album:
            if row_album == cand_album:
                score += 10
            elif row_album in cand_album or cand_album in row_album:
                score += 6

    score += _year_bonus(row.release_date, candidate_date)
    score -= _variant_penalty(row.track_name, candidate_title)
    if row.duration_ms and candidate_duration_ms:
        delta = abs(row.duration_ms - candidate_duration_ms)
        if delta <= 2500:
            score += 8
        elif delta <= 6000:
            score += 4
        elif delta >= 25000:
            score -= 16

    return max(score, 1)


def score_candidate_repair(
    row: "ExportifyCsvRow",
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_date: str,
    candidate_isrc: str,
    candidate_duration_ms: int | None = None,
) -> int:
    """Extended scoring for repair-mode candidate matching.

    Tries the standard :func:`score_candidate` first; if that returns 0 (no
    title match), falls back to a fuzzy title comparison using
    :class:`difflib.SequenceMatcher`.  The fuzzy score only activates if the
    normalised title similarity ratio is ≥ 0.80 so clearly wrong results are
    not promoted.

    Fuzzy scoring tiers (applied only when the standard scorer returns 0):
    - Fuzzy ratio ≥ 0.80 + artist overlap → 35
    - Fuzzy ratio ≥ 0.80 + album partial match → 28
    - Fuzzy ratio ≥ 0.80 only → 20
    - Release year bonus (+5) applied on top of fuzzy tier

    ISRC match still short-circuits to 100 (handled inside
    :func:`score_candidate`).
    """
    std = score_candidate(
        row,
        candidate_title,
        candidate_artist,
        candidate_album,
        candidate_date,
        candidate_isrc,
        candidate_duration_ms,
    )
    if std > 0:
        return std

    # Fuzzy fallback — repair mode only
    norm_title = _normalise(row.track_name)
    norm_cand = _normalise(candidate_title)
    if not norm_title or not norm_cand:
        return 0

    ratio = SequenceMatcher(None, norm_title, norm_cand).ratio()
    if ratio < 0.80:
        return 0

    if _artist_overlap(row.artists_list, candidate_artist):
        score = 35
    elif row.album and _normalise(row.album) in _normalise(candidate_album):
        score = 28
    else:
        score = 20

    if row.release_date and candidate_date:
        if row.release_date[:4] == str(candidate_date)[:4]:
            score += 5

    return score


def is_usable_exportify_row(row: ExportifyCsvRow) -> bool:
    """Whether a CSV row has enough base identity fields to be resolved."""
    return bool(row.track_name.strip()) and bool(
        row.artists_raw.strip() or row.artists_list
    )


def parse_unresolved_csv(path: str) -> list[ExportifyCsvRow]:
    """Parse an ``*_unresolved.csv`` log file back into :class:`ExportifyCsvRow` objects.

    The unresolved log (written by :class:`~streamrip.db.UnresolvedQueryLog`)
    stores all fields needed to replay the query.

    Unknown columns are ignored; missing optional fields default to empty string.
    """
    rows: list[ExportifyCsvRow] = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader):
            artists_raw = (row.get("artists") or "").strip()
            artists_list = [a.strip() for a in artists_raw.split(";") if a.strip()]

            try:
                source_row_index = int(
                    row.get("source_row_index") or row.get("row_index") or i
                )
            except (ValueError, TypeError):
                source_row_index = i
            try:
                position = int(
                    row.get("original_position")
                    or row.get("position")
                    or source_row_index + 1
                )
            except (ValueError, TypeError):
                position = i + 1
            try:
                duration_ms = int((row.get("duration_ms") or "").strip() or 0) or None
            except (ValueError, TypeError):
                duration_ms = None

            repair_candidate_ids: dict[str, str] | None = None
            primary_source = (row.get("primary_source") or "").strip()
            fallback_source = (row.get("fallback_source") or "").strip()
            primary_candidate_id = (row.get("primary_candidate_id") or "").strip()
            fallback_candidate_id = (row.get("fallback_candidate_id") or "").strip()
            if primary_source and primary_candidate_id:
                repair_candidate_ids = {primary_source: primary_candidate_id}
            if fallback_source and fallback_candidate_id:
                if repair_candidate_ids is None:
                    repair_candidate_ids = {}
                repair_candidate_ids[fallback_source] = fallback_candidate_id

            rows.append(
                ExportifyCsvRow(
                    track_name=(row.get("track_name") or "").strip(),
                    artists_raw=artists_raw,
                    artists_list=artists_list,
                    album=(row.get("album") or "").strip(),
                    release_date=(row.get("release_date") or "").strip(),
                    isrc=(row.get("isrc") or "").strip(),
                    spotify_uri=(row.get("spotify_uri") or "").strip(),
                    genres="",
                    loudness="",
                    tempo="",
                    position=position,
                    row_index=source_row_index,
                    source_row_index=source_row_index,
                    canonical_track_name=strip_title_decorators(
                        (row.get("track_name") or "").strip()
                    ),
                    duration_ms=duration_ms,
                    repair_candidate_ids=repair_candidate_ids,
                )
            )
    return rows


def _duration_match_with_tolerance(
    expected_ms: int | None,
    actual_seconds: float | None,
    *,
    tolerance_ratio: float,
    tolerance_seconds: float,
) -> bool:
    """CSV duration sanity check with hybrid tolerance and anti-preview guard."""
    if expected_ms is None or expected_ms <= 0:
        return True
    if actual_seconds is None or actual_seconds <= 0:
        # Best-effort only: inability to read duration should not fail resolution.
        return True
    expected_seconds = expected_ms / 1000.0
    if actual_seconds < 45.0 and expected_seconds >= 90.0:
        return False
    allowed_delta = max(
        float(tolerance_seconds), expected_seconds * float(tolerance_ratio)
    )
    return abs(actual_seconds - expected_seconds) <= allowed_delta
