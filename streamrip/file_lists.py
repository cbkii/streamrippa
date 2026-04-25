"""File list parsing utilities for the ``rip file`` command.

Supports three modes:

- json: list of ``{"source", "media_type", "id"}`` objects
- exportify-csv: Exportify-format CSV export from Spotify
- urls: whitespace-separated service URLs (default/fallback)
"""

from __future__ import annotations

import csv
import functools
import json
import logging
import os
import re
import warnings
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

logger = logging.getLogger("streamrip")

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


@dataclass(slots=True, frozen=True)
class ParsedTitle:
    original: str
    normalized: str
    core_title: str
    variants: frozenset[str]


@dataclass(slots=True)
class MatchPolicy:
    enabled: bool = True
    live_mode: str = "reject"
    acoustic_mode: str = "reject"
    instrumental_mode: str = "reject"
    radio_edit_mode: str = "penalty"
    remaster_mode: str = "equivalent"
    year_ignore_for_remaster: bool = True
    reject_bad_context_releases: bool = True
    bad_context_fields: tuple[str, ...] = ("title", "album")
    enable_guarded_fuzzy_normal: bool = False

    @classmethod
    def from_config(cls, config) -> "MatchPolicy":
        if config is None:
            return cls()
        return cls(
            enabled=bool(getattr(config, "variant_policy_enabled", True)),
            live_mode=str(getattr(config, "live_mode", "reject")),
            acoustic_mode=str(getattr(config, "acoustic_mode", "reject")),
            instrumental_mode=str(getattr(config, "instrumental_mode", "reject")),
            radio_edit_mode=str(getattr(config, "radio_edit_mode", "penalty")),
            remaster_mode=str(getattr(config, "remaster_mode", "equivalent")),
            year_ignore_for_remaster=bool(
                getattr(config, "year_ignore_for_remaster", True)
            ),
            reject_bad_context_releases=bool(
                getattr(config, "reject_bad_context_releases", True)
            ),
            bad_context_fields=_resolve_bad_context_fields(
                tuple(getattr(config, "bad_context_fields", ("title", "album")) or ())
            ),
            enable_guarded_fuzzy_normal=bool(
                getattr(config, "enable_guarded_fuzzy_normal", False)
            ),
        )


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


_VARIANT_ALIASES: dict[str, tuple[str, ...]] = {
    "live": ("live", "live at", "in concert"),
    "acoustic": ("acoustic", "unplugged"),
    "instrumental": ("instrumental",),
    "radio_edit": ("radio edit", "radio mix", "single edit", "short version"),
    "remaster": ("remaster", "remastered", "anniversary remaster"),
    "remix": ("remix",),
    "demo": ("demo",),
    "explicit": ("explicit",),
    "clean": ("clean",),
    "mono": ("mono",),
    "stereo": ("stereo",),
    "orchestral": ("orchestral", "orchestra version"),
    "session": ("session", "take"),
    "rehearsal": ("rehearsal",),
    "commentary": ("commentary",),
    "karaoke": ("karaoke", "instrumental karaoke"),
    "tribute": ("tribute", "made famous by"),
    "slowed_sped": ("slowed", "sped up", "reverb"),
}
_BAD_CONTEXT_MARKERS: tuple[str, ...] = (
    "karaoke",
    "tribute",
    "made famous by",
    "commentary",
    "podcast",
    "audiobook",
    "lullaby",
    "meditation",
    "workout",
    "8d audio",
    "nightcore",
    "slowed",
    "sped up",
    "reverb",
)
# Variants that allow a bad-context candidate to still be accepted when the
# CSV row itself explicitly requests one of these types.
_BAD_CONTEXT_CARVEOUT_VARIANTS: frozenset[str] = frozenset(
    {"karaoke", "tribute", "commentary", "slowed_sped"}
)
_BAD_CONTEXT_SUPPORTED_FIELDS: frozenset[str] = frozenset(
    {"title", "album", "artist", "version", "subtitle", "display_title"}
)
# Pre-compiled pattern for bad-context marker scanning; sorted longest-first so
# multi-word phrases (e.g. "made famous by") are tried before their substrings.
_BAD_CONTEXT_PATTERN: re.Pattern[str] = re.compile(
    r"\b("
    + "|".join(
        re.escape(m) for m in sorted(_BAD_CONTEXT_MARKERS, key=len, reverse=True)
    )
    + r")\b"
)


@dataclass(slots=True, frozen=True)
class CandidateExplanation:
    score: int
    reason_codes: tuple[str, ...]
    signals: dict[str, object]


_CORE_STRIP_MARKERS: frozenset[str] = frozenset(
    {
        "live",
        "acoustic",
        "instrumental",
        "radio edit",
        "single edit",
        "edit",
        "remaster",
        "remastered",
        "mono",
        "stereo",
        "explicit",
        "clean",
        "version",
        "mix",
        "demo",
        "session",
        "unplugged",
        "bonus track",
        "feat",
        "featuring",
        "ft",
    }
)


def _normalise_variant_text(s: str) -> str:
    """Normalize and remove common variant markers for coarse fallback matching."""
    norm = _normalise(strip_title_decorators(s))
    if not norm:
        return ""
    marker_pattern = "|".join(
        sorted(
            (re.escape(marker) for marker in _CORE_STRIP_MARKERS), key=len, reverse=True
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


def _extract_variant_markers(text: str) -> frozenset[str]:
    norm = _normalise(text)
    if not norm:
        return frozenset()
    markers: list[str] = []
    for canonical, aliases in _VARIANT_ALIASES.items():
        if any(re.search(rf"\b{re.escape(alias)}\b", norm) for alias in aliases):
            markers.append(canonical)
    return frozenset(sorted(markers))


def _parse_title(text: str) -> ParsedTitle:
    norm = _normalise(text or "")
    markers = _extract_variant_markers(text or "")
    core = _normalise_variant_text(text or "")
    if not core:
        core = norm
    return ParsedTitle(
        original=text or "",
        normalized=norm,
        core_title=core,
        variants=markers,
    )


def _contains_bad_context(title: str, album: str, artist: str) -> bool:
    full = _normalise(" ".join([title or "", album or "", artist or ""]))
    if not full:
        return False
    return bool(_BAD_CONTEXT_PATTERN.search(full))


@functools.lru_cache(maxsize=64)
def _resolve_bad_context_fields(
    config_fields: tuple[str, ...] | None,
) -> tuple[str, ...]:
    env_override = (os.getenv("STREAMRIP_BAD_CONTEXT_FIELDS") or "").strip()
    raw_fields: list[str]
    if env_override:
        raw_fields = [
            p.strip().casefold() for p in env_override.split(",") if p.strip()
        ]
    else:
        raw_fields = [
            str(v).strip().casefold() for v in (config_fields or ()) if str(v).strip()
        ]
    if not raw_fields:
        return ("title", "album")

    resolved: list[str] = []
    for field in raw_fields:
        if field not in _BAD_CONTEXT_SUPPORTED_FIELDS:
            logger.warning("Ignoring unknown bad-context field '%s'", field)
            continue
        if field not in resolved:
            resolved.append(field)
    return tuple(resolved) if resolved else ("title", "album")


def _contains_bad_context_fields(
    candidate_title: str,
    candidate_album: str,
    candidate_artist: str,
    *,
    bad_context_fields: tuple[str, ...],
) -> bool:
    # "version", "subtitle", and "display_title" are all facets of the
    # track title field in different provider schemas; they share the same
    # backing value so the deduplication loop below avoids scanning it twice.
    field_values: dict[str, str] = {
        "title": candidate_title,
        "album": candidate_album,
        "version": candidate_title,
        "subtitle": candidate_title,
        "display_title": candidate_title,
        "artist": candidate_artist,
    }
    # Deduplicate backing values so the same string isn't scanned multiple times
    # (e.g. "title", "version", and "subtitle" all resolve to candidate_title).
    seen_values: set[str] = set()
    parts: list[str] = []
    for field in bad_context_fields:
        value = field_values.get(field, "")
        if value and value not in seen_values:
            seen_values.add(value)
            parts.append(value)
    full = _normalise(" ".join(parts))
    if not full:
        return False
    return bool(_BAD_CONTEXT_PATTERN.search(full))


def _variant_mode(policy: MatchPolicy, marker: str) -> str:
    if marker == "live":
        return policy.live_mode
    if marker == "acoustic":
        return policy.acoustic_mode
    if marker == "instrumental":
        return policy.instrumental_mode
    if marker == "radio_edit":
        return policy.radio_edit_mode
    if marker == "remaster":
        return policy.remaster_mode
    # Other material variants default to reject unless explicitly expected by source.
    if marker in {"remix", "demo", "orchestral", "session", "rehearsal"}:
        return "reject"
    # explicit/clean/mono/stereo are near-equivalent.
    if marker in {"explicit", "clean", "mono", "stereo"}:
        return "equivalent"
    return "penalty"


def _variant_policy_penalty(
    expected: frozenset[str],
    candidate: frozenset[str],
    policy: MatchPolicy,
) -> tuple[int, bool]:
    if not policy.enabled:
        return 0, False
    penalty = 0
    reject = False
    unexpected = sorted(v for v in candidate if v not in expected)
    missing_expected = sorted(v for v in expected if v not in candidate)

    for marker in unexpected:
        mode = _variant_mode(policy, marker)
        if mode == "reject":
            reject = True
        elif mode == "penalty":
            penalty += 8

    for marker in missing_expected:
        # Expected variant absent in candidate is a strong mismatch.
        mode = _variant_mode(policy, marker)
        if mode in {"reject", "penalty"}:
            penalty += 10
            if marker in {
                "live",
                "acoustic",
                "instrumental",
                "remix",
                "demo",
                "orchestral",
                "session",
                "rehearsal",
            }:
                reject = True
    return penalty, reject


def _score_candidate_internal(
    row: "ExportifyCsvRow",
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_date: str,
    candidate_isrc: str,
    candidate_duration_ms: int | None = None,
    policy: MatchPolicy | None = None,
    *,
    allow_guarded_fuzzy: bool = True,
) -> CandidateExplanation:
    policy = policy or MatchPolicy()
    reasons: list[str] = []
    row_title = _parse_title(row.track_name)
    cand_title = _parse_title(candidate_title)
    signals: dict[str, object] = {
        "isrc_match": bool(
            row.isrc and candidate_isrc and row.isrc.upper() == candidate_isrc.upper()
        ),
        "row_variants": sorted(row_title.variants),
        "candidate_variants": sorted(cand_title.variants),
        "title_exact": False,
        "title_core": False,
        "title_fuzzy_guarded": False,
    }

    if row.isrc and candidate_isrc and row.isrc.upper() == candidate_isrc.upper():
        has_bad_context = (
            policy.enabled
            and policy.reject_bad_context_releases
            and _contains_bad_context_fields(
                candidate_title,
                candidate_album,
                candidate_artist,
                bad_context_fields=policy.bad_context_fields,
            )
        )
        if has_bad_context and not row_title.variants.intersection(
            _BAD_CONTEXT_CARVEOUT_VARIANTS
        ):
            return CandidateExplanation(
                score=0,
                reason_codes=("reject_bad_context",),
                signals=signals,
            )
        return CandidateExplanation(
            score=100,
            reason_codes=("accepted_isrc_match",),
            signals=signals,
        )

    if not row_title.normalized or not cand_title.normalized:
        return CandidateExplanation(
            score=0, reason_codes=("reject_empty_title",), signals=signals
        )

    exact_title = row_title.normalized == cand_title.normalized
    core_title_match = (
        bool(row_title.core_title)
        and bool(cand_title.core_title)
        and row_title.core_title == cand_title.core_title
    )
    signals["title_exact"] = exact_title
    signals["title_core"] = core_title_match
    guarded_fuzzy_match = False
    if not exact_title and not core_title_match:
        if allow_guarded_fuzzy and policy.enable_guarded_fuzzy_normal:
            fuzzy_ratio = SequenceMatcher(
                None, row_title.normalized, cand_title.normalized
            ).ratio()
            signals["title_similarity"] = round(fuzzy_ratio, 4)
            artist_ok = _artist_overlap(row.artists_list, candidate_artist)
            album_ok = bool(
                row.album
                and candidate_album
                and _normalise_variant_text(row.album)
                and (
                    _normalise_variant_text(row.album)
                    in _normalise_variant_text(candidate_album)
                    or _normalise_variant_text(candidate_album)
                    in _normalise_variant_text(row.album)
                )
            )
            duration_ok = bool(
                row.duration_ms
                and candidate_duration_ms
                and abs(row.duration_ms - candidate_duration_ms) <= 8000
            )
            guarded_fuzzy_match = (
                fuzzy_ratio >= 0.90 and artist_ok and (album_ok or duration_ok)
            )
            signals["title_fuzzy_guarded"] = guarded_fuzzy_match
        if not guarded_fuzzy_match:
            return CandidateExplanation(
                score=0, reason_codes=("reject_title_mismatch",), signals=signals
            )

    if (
        policy.enabled
        and policy.reject_bad_context_releases
        and _contains_bad_context_fields(
            candidate_title,
            candidate_album,
            candidate_artist,
            bad_context_fields=policy.bad_context_fields,
        )
    ):
        if not row_title.variants.intersection(_BAD_CONTEXT_CARVEOUT_VARIANTS):
            return CandidateExplanation(
                score=0, reason_codes=("reject_bad_context",), signals=signals
            )

    score = 27
    if exact_title:
        score += 10
    if core_title_match:
        score += 8
    if guarded_fuzzy_match:
        score += 6
        reasons.append("accepted_guarded_fuzzy")

    coverage = _artist_coverage(row.artists_list, candidate_artist)
    signals["artist_coverage"] = round(coverage, 3)
    if coverage >= 1.0:
        score += 26
    elif coverage >= 0.5:
        score += 14
    elif _artist_overlap(row.artists_list, candidate_artist):
        score += 8
    else:
        weak_context = False
        if row.album and candidate_album and (core_title_match or guarded_fuzzy_match):
            row_album_norm = _normalise_variant_text(row.album)
            cand_album_norm = _normalise_variant_text(candidate_album)
            weak_context = bool(
                row_album_norm
                and cand_album_norm
                and (
                    row_album_norm == cand_album_norm
                    or row_album_norm in cand_album_norm
                    or cand_album_norm in row_album_norm
                )
            )
        if not weak_context:
            return CandidateExplanation(
                score=0, reason_codes=("reject_artist_mismatch",), signals=signals
            )
        score -= 10
        reasons.append("penalty_weak_artist_context")

    if row.album and candidate_album:
        row_album = _normalise_variant_text(row.album)
        cand_album = _normalise_variant_text(candidate_album)
        if row_album and cand_album:
            if row_album == cand_album:
                score += 4
            elif row_album in cand_album or cand_album in row_album:
                score += 2

    _year_ignore_variants = ("remaster", "live", "remix")
    row_has_variant = any(v in row_title.variants for v in _year_ignore_variants)
    cand_has_variant = any(v in cand_title.variants for v in _year_ignore_variants)
    if not (
        policy.enabled
        and policy.year_ignore_for_remaster
        and (row_has_variant or cand_has_variant)
    ):
        score += _year_bonus(row.release_date, candidate_date)

    variant_penalty, reject_variant = _variant_policy_penalty(
        row_title.variants,
        cand_title.variants,
        policy,
    )
    signals["variant_penalty"] = variant_penalty
    if reject_variant and row_title.variants != cand_title.variants:
        return CandidateExplanation(
            score=0, reason_codes=("reject_variant_policy",), signals=signals
        )
    if row_title.variants and row_title.variants.issubset(cand_title.variants):
        score += 8
    score -= variant_penalty

    if row.duration_ms and candidate_duration_ms:
        delta = abs(row.duration_ms - candidate_duration_ms)
        signals["duration_delta_ms"] = delta
        if delta <= 2500:
            score += 10
        elif delta <= 6000:
            score += 4
        elif delta <= 12000:
            score += 1
        elif delta >= 25000:
            return CandidateExplanation(
                score=0, reason_codes=("reject_duration_far",), signals=signals
            )
        elif delta >= 15000:
            score -= 14
        else:
            score -= 6
    return CandidateExplanation(
        score=max(score, 1), reason_codes=tuple(reasons), signals=signals
    )


def score_candidate(
    row: "ExportifyCsvRow",
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_date: str,
    candidate_isrc: str,
    candidate_duration_ms: int | None = None,
    policy: MatchPolicy | None = None,
) -> int:
    """
    Score how well a search-result candidate matches a CSV row from an Exportify export.

    Uses deterministic heuristics combining ISRC, title, artist, album and release year:
    - Exact ISRC match yields 100.
    - Requires either exact normalised title match or exact normalised-variant title match to produce a non-zero score.
    - Base score for title match is 27, with bonuses for exact normalised title, artist coverage, album match, and year; penalties for variant/edition mismatches.
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
    return _score_candidate_internal(
        row,
        candidate_title,
        candidate_artist,
        candidate_album,
        candidate_date,
        candidate_isrc,
        candidate_duration_ms,
        policy=policy,
        allow_guarded_fuzzy=True,
    ).score


def score_candidate_repair(
    row: "ExportifyCsvRow",
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_date: str,
    candidate_isrc: str,
    candidate_duration_ms: int | None = None,
    policy: MatchPolicy | None = None,
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
    policy = policy or MatchPolicy()
    std = score_candidate(
        row,
        candidate_title,
        candidate_artist,
        candidate_album,
        candidate_date,
        candidate_isrc,
        candidate_duration_ms,
        policy=policy,
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

    # Re-apply policy rejections so the fuzzy path cannot bypass them.
    if policy.enabled:
        row_parsed = _parse_title(row.track_name)
        cand_parsed = _parse_title(candidate_title)
        _, reject_variant = _variant_policy_penalty(
            row_parsed.variants, cand_parsed.variants, policy
        )
        if reject_variant and row_parsed.variants != cand_parsed.variants:
            return 0
        if policy.reject_bad_context_releases and _contains_bad_context_fields(
            candidate_title,
            candidate_album,
            candidate_artist,
            bad_context_fields=policy.bad_context_fields,
        ):
            if not row_parsed.variants.intersection(_BAD_CONTEXT_CARVEOUT_VARIANTS):
                return 0

    return score


def explain_candidate_score(
    row: "ExportifyCsvRow",
    candidate_title: str,
    candidate_artist: str,
    candidate_album: str,
    candidate_date: str,
    candidate_isrc: str,
    candidate_duration_ms: int | None = None,
    policy: MatchPolicy | None = None,
) -> CandidateExplanation:
    return _score_candidate_internal(
        row,
        candidate_title,
        candidate_artist,
        candidate_album,
        candidate_date,
        candidate_isrc,
        candidate_duration_ms,
        policy=policy,
        allow_guarded_fuzzy=True,
    )


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
            override_primary_source = (row.get("override_primary_source") or "").strip()
            override_primary_id = (
                row.get("override_primary_candidate_id") or ""
            ).strip()
            override_fallback_source = (
                row.get("override_fallback_source") or ""
            ).strip()
            override_fallback_id = (
                row.get("override_fallback_candidate_id") or ""
            ).strip()
            _track_name_hint = (row.get("track_name") or "").strip()

            def _warn_incomplete_override(kind: str, src: str, cand_id: str) -> None:
                logger.warning(
                    "Row %d (%r): incomplete %s override "
                    "(override_%s_source=%r, override_%s_candidate_id=%r); "
                    "ignoring override and keeping original %s fields",
                    source_row_index,
                    _track_name_hint,
                    kind,
                    kind,
                    src,
                    kind,
                    cand_id,
                    kind,
                )

            if override_primary_source and override_primary_id:
                primary_source = override_primary_source
                primary_candidate_id = override_primary_id
            elif override_primary_source or override_primary_id:
                _warn_incomplete_override(
                    "primary", override_primary_source, override_primary_id
                )
            if override_fallback_source and override_fallback_id:
                fallback_source = override_fallback_source
                fallback_candidate_id = override_fallback_id
            elif override_fallback_source or override_fallback_id:
                _warn_incomplete_override(
                    "fallback", override_fallback_source, override_fallback_id
                )
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
    """CSV duration sanity check with hybrid tolerance and anti-preview guard.

    Returns False when ``actual_seconds`` is unavailable (None or <= 0) and
    ``expected_ms`` is positive.  Callers that want best-effort / permissive
    behaviour for unreadable files must guard before calling this function.
    """
    if expected_ms is None or expected_ms <= 0:
        return True
    if actual_seconds is None or actual_seconds <= 0:
        return False
    expected_seconds = expected_ms / 1000.0
    if actual_seconds < 45.0 and expected_seconds >= 90.0:
        return False
    allowed_delta = max(
        float(tolerance_seconds), expected_seconds * float(tolerance_ratio)
    )
    return abs(actual_seconds - expected_seconds) <= allowed_delta
