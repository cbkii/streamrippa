from __future__ import annotations

from pathlib import Path

from streamrip.file_lists import (
    ExportifyCsvRow,
    MatchPolicy,
    explain_candidate_score,
    parse_exportify_csv,
)
from streamrip.media.csv_playlist import (
    TrackCandidate,
    _build_search_queries,
    _select_best_candidate,
)


def _row(**overrides) -> ExportifyCsvRow:
    values = {
        "track_name": "Hard Twelve",
        "artists_raw": "Beat Assailant",
        "artists_list": ["Beat Assailant"],
        "album": "Hard Twelve",
        "release_date": "2005-01-01",
        "isrc": "",
        "spotify_uri": "spotify:track:test",
        "genres": "",
        "loudness": "",
        "tempo": "",
        "position": 1,
        "row_index": 0,
        "source_row_index": 0,
        "canonical_track_name": "Hard Twelve",
        "duration_ms": 239000,
        "repair_candidate_ids": None,
    }
    values.update(overrides)
    return ExportifyCsvRow(**values)


def test_normal_query_plan_broadens_without_requiring_year() -> None:
    row = _row(album="Imperial Pressure", release_date="2005-08-22")

    queries = _build_search_queries(row, "deezer", escalation=False)
    strategies = [strategy for strategy, _ in queries]
    query_map = dict(queries)

    assert "title-artist-album" in strategies
    assert "title-artist" in strategies
    assert "artist-title" in strategies
    assert "title-album" in strategies
    assert "title-only" in strategies
    assert "2005" not in query_map["title-artist-album"]
    assert strategies.index("title-artist") < strategies.index("title-only")
    assert len([q.casefold() for _, q in queries]) == len(
        set(q.casefold() for _, q in queries)
    )


def test_hard_twelve_provider_subtitle_is_guardedly_accepted() -> None:
    explanation = explain_candidate_score(
        _row(album="Imperial Pressure"),
        "Hard Twelve (The Ante)",
        "Beat Assailant",
        "Imperial Pressure",
        "2005-08-22",
        "",
        239500,
        policy=MatchPolicy(enable_guarded_fuzzy_normal=False),
    )

    assert explanation.score >= 50
    assert "accepted_neutral_title_extension" in explanation.reason_codes
    assert explanation.signals["title_neutral_extension"] is True


def test_dash_subtitle_variant_is_guardedly_accepted() -> None:
    explanation = explain_candidate_score(
        _row(album="Imperial Pressure"),
        "Hard Twelve - The Ante",
        "Beat Assailant",
        "Imperial Pressure",
        "2005-08-22",
        "",
        240000,
        policy=MatchPolicy(enable_guarded_fuzzy_normal=False),
    )

    assert explanation.score >= 50
    assert explanation.signals["title_neutral_extension"] is True


def test_incompatible_live_variant_is_not_neutral_extension() -> None:
    explanation = explain_candidate_score(
        _row(album="Imperial Pressure"),
        "Hard Twelve (Live)",
        "Beat Assailant",
        "Imperial Pressure",
        "2005-08-22",
        "",
        239000,
        policy=MatchPolicy(enable_guarded_fuzzy_normal=True),
    )

    assert explanation.score == 0
    assert "reject_variant_policy" in explanation.reason_codes or "reject_title_mismatch" in explanation.reason_codes


def test_exact_isrc_allows_reissue_album_difference() -> None:
    explanation = explain_candidate_score(
        _row(isrc="FRABC0512345", album="Original Album"),
        "Hard Twelve",
        "Beat Assailant",
        "Later Compilation",
        "2018-01-01",
        "frabc0512345",
        239500,
        policy=MatchPolicy(),
    )

    assert explanation.score == 100
    assert "accepted_isrc_match" in explanation.reason_codes


def test_exact_isrc_rejects_severe_artist_conflict() -> None:
    explanation = explain_candidate_score(
        _row(isrc="FRABC0512345"),
        "Hard Twelve",
        "Completely Different Artist",
        "Hard Twelve",
        "2005-01-01",
        "FRABC0512345",
        239000,
        policy=MatchPolicy(),
    )

    assert explanation.score == 0
    assert "reject_isrc_artist_conflict" in explanation.reason_codes


def test_exact_isrc_rejects_severe_duration_conflict() -> None:
    explanation = explain_candidate_score(
        _row(isrc="FRABC0512345", duration_ms=239000),
        "Hard Twelve",
        "Beat Assailant",
        "Hard Twelve",
        "2005-01-01",
        "FRABC0512345",
        290000,
        policy=MatchPolicy(),
    )

    assert explanation.score == 0
    assert "reject_isrc_duration_conflict" in explanation.reason_codes


def test_candidate_aggregation_prefers_later_stronger_match() -> None:
    class _Client:
        source = "qobuz"

    client = _Client()
    first = TrackCandidate(
        source="qobuz",
        id="first",
        title="Song",
        artist="Artist",
        album="Album",
        release_date="",
        isrc="",
        score=52,
        client=client,  # type: ignore[arg-type]
    )
    later = TrackCandidate(
        source="qobuz",
        id="later",
        title="Song",
        artist="Artist",
        album="Album",
        release_date="",
        isrc="",
        score=88,
        client=client,  # type: ignore[arg-type]
    )

    selected = _select_best_candidate(
        [("early", "Song Artist", first), ("later", "Artist Song", later)]
    )

    assert selected is not None
    assert selected[2].id == "later"
    assert selected[2].score == 88


def test_exportify_parser_accepts_common_column_aliases(tmp_path: Path) -> None:
    csv_path = tmp_path / "aliases.csv"
    csv_path.write_text(
        "Track URI,Track Name,Artist Name(s),Album Name,Album Release Date,Track Duration (ms),Artist Genres,Track ISRC\n"
        'spotify:track:1,Hard Twelve,Beat Assailant,Imperial Pressure,2005-08-22,239000,"Hip Hop, Soul",FRABC0512345\n',
        encoding="utf-8",
    )

    _, rows = parse_exportify_csv(str(csv_path))

    assert len(rows) == 1
    row = rows[0]
    assert row.release_date == "2005-08-22"
    assert row.duration_ms == 239000
    assert row.genres == "Hip Hop, Soul"
    assert row.isrc == "FRABC0512345"
