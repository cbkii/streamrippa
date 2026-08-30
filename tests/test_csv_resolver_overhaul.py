from __future__ import annotations

from pathlib import Path

from streamrip.file_lists import (
    ExportifyCsvRow,
    MatchPolicy,
    explain_candidate_score,
    explain_candidate_score_repair,
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

    assert "structured" in strategies
    assert "generic" in strategies
    assert "artist-title" in strategies
    assert "title-album" in strategies
    assert "title-only" in strategies
    assert "2005" not in query_map["structured"]
    assert strategies.index("generic") < strategies.index("title-only")
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
    assert (
        "reject_variant_policy" in explanation.reason_codes
        or "reject_title_mismatch" in explanation.reason_codes
    )


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


def test_exportify_parser_recognises_comma_separated_artist_credits(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "artists.csv"
    csv_path.write_text(
        "Track URI,Track Name,Artist Name(s),Album Name\n"
        'spotify:track:1,Good Day,"Greg Street, Nappy Roots",Good Day\n',
        encoding="utf-8",
    )

    _, rows = parse_exportify_csv(str(csv_path))

    assert rows[0].artists_raw == "Greg Street, Nappy Roots"
    assert rows[0].artists_list == ["Greg Street", "Nappy Roots"]


def test_repair_magic_number_catalogue_prefix_can_clear_strict_threshold() -> None:
    explanation = explain_candidate_score_repair(
        _row(
            track_name="Magic Number",
            canonical_track_name="Magic Number",
            artists_raw="De La Soul",
            artists_list=["De La Soul"],
            album="3 Feet High and Rising",
            duration_ms=207000,
        ),
        "(3 Is) The Magic Number",
        "De La Soul",
        "3 Feet High and Rising",
        "1989-03-03",
        "",
        207500,
        policy=MatchPolicy(),
    )

    assert explanation.score >= 50
    assert explanation.signals["title_neutral_extension"] is True


def test_repair_awnaw_compact_title_can_clear_strict_threshold() -> None:
    explanation = explain_candidate_score_repair(
        _row(
            track_name="Aw Naw",
            canonical_track_name="Aw Naw",
            artists_raw="Nappy Roots",
            artists_list=["Nappy Roots"],
            album="Watermelon, Chicken & Gritz",
            duration_ms=239000,
        ),
        "Awnaw",
        "Nappy Roots",
        "Watermelon, Chicken & Gritz",
        "2002-02-26",
        "",
        239700,
        policy=MatchPolicy(),
    )

    assert explanation.score >= 50
    assert "accepted_repair_compact_title" in explanation.reason_codes


def test_unbracketed_feature_credit_is_removed_from_csv_canonical_title(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "featured.csv"
    csv_path.write_text(
        "Track URI,Track Name,Artist Name(s),Album Name\n"
        'spotify:track:1,"So Far To Go feat. Common & D\'Angelo",J Dilla,The Shining\n',
        encoding="utf-8",
    )

    _, rows = parse_exportify_csv(str(csv_path))

    assert rows[0].canonical_track_name == "So Far To Go"


def test_diacritic_artist_difference_is_not_an_artist_mismatch() -> None:
    explanation = explain_candidate_score(
        _row(
            track_name="Take It Easy My Brother Charles",
            canonical_track_name="Take It Easy My Brother Charles",
            artists_raw="Som Tres",
            artists_list=["Som Tres"],
            album="Som 3",
            duration_ms=183000,
        ),
        "Take It Easy My Brother Charles",
        "Som Três",
        "Som 3",
        "1970-01-01",
        "",
        183500,
        policy=MatchPolicy(),
    )

    assert explanation.score >= 50
    assert explanation.signals["artist_coverage"] == 1.0


def test_repair_fuzzy_unrelated_artist_stays_below_strict_threshold() -> None:
    explanation = explain_candidate_score_repair(
        _row(
            track_name="Can't Help Loving That Man",
            canonical_track_name="Can't Help Loving That Man",
            artists_raw="Trudy Richards",
            artists_list=["Trudy Richards"],
            album="The Many Moods of Trudy Richards",
            duration_ms=180000,
        ),
        "Can't Help Lovin' That Man",
        "Different Singer",
        "The Many Moods of Trudy Richards",
        "1957-01-01",
        "",
        180500,
        policy=MatchPolicy(),
    )

    assert 0 < explanation.score < 50
    assert "repair_low_confidence_fuzzy" in explanation.reason_codes


def test_repair_weak_fuzzy_without_recording_context_stays_low_score() -> None:
    explanation = explain_candidate_score_repair(
        _row(
            track_name="Heatwave Moving",
            canonical_track_name="Heatwave Moving",
            artists_raw="Tommy McCook",
            artists_list=["Tommy McCook"],
            album="",
            duration_ms=None,
        ),
        "Heatwave aka Moving",
        "Tommy McCook",
        "",
        "",
        "",
        None,
        policy=MatchPolicy(),
    )

    assert 0 < explanation.score < 50
    assert "repair_low_confidence_fuzzy" in explanation.reason_codes


def test_repair_does_not_semantically_guess_different_de_la_soul_title() -> None:
    explanation = explain_candidate_score_repair(
        _row(
            track_name="It Aint All Good",
            canonical_track_name="It Aint All Good",
            artists_raw="De La Soul",
            artists_list=["De La Soul"],
            album="",
            duration_ms=None,
        ),
        "All Good?",
        "De La Soul",
        "Art Official Intelligence: Mosaic Thump",
        "2000-08-08",
        "",
        240000,
        policy=MatchPolicy(),
    )

    assert explanation.score == 0
