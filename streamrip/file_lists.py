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
    # Optional provider-ID hints from unresolved CSV logs: {source: track_id}
    repair_candidate_ids: dict[str, str] | None = None


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

            rows.append(
                ExportifyCsvRow(
                    track_name=(row.get("Track Name") or "").strip(),
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
                )
            )

    return playlist_name, rows


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
    """Return a normalised string with common edition/version markers removed."""
    norm = _normalise(s)
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
) -> int:
    """Deterministically score a search-result candidate against a CSV row.

    Scoring rules (higher = better match):
    - Exact ISRC match → 100 (short-circuits other checks)
    - Normalised title match + artist overlap → 60
    - Normalised title match + album partial match → 50
    - Normalised title match only → 40
    - Release year bonus (+5) applied on top

    Returns:
        Integer score; 0 means no title match.
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

    return max(score, 1)


def score_candidate_repair(
    row: "ExportifyCsvRow",
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_date: str,
    candidate_isrc: str,
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
                position = int(row.get("row_index") or i) + 1
            except (ValueError, TypeError):
                position = i + 1

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
                    row_index=i,
                    repair_candidate_ids=repair_candidate_ids,
                )
            )
    return rows
