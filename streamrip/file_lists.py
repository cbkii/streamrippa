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
    titles_match = bool(norm_title) and norm_title == norm_cand

    score = 0
    if titles_match:
        if _artist_overlap(row.artists_list, candidate_artist):
            score = 60
        elif row.album and _normalise(row.album) in _normalise(candidate_album):
            score = 50
        else:
            score = 40

    # Release year bonus
    if score > 0 and row.release_date and candidate_date:
        if row.release_date[:4] == str(candidate_date)[:4]:
            score += 5

    return score
