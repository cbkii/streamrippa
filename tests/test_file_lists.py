"""Tests for streamrip.file_lists — CSV parser and mode detection."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from streamrip.file_lists import (
    ExportifyCsvRow,
    MatchPolicy,
    _artist_overlap,
    _normalise,
    _normalise_variant_text,
    detect_file_mode,
    exportify_artist_group_key,
    is_usable_exportify_row,
    parse_exportify_csv,
    partition_exportify_rows_artist_batched,
    score_candidate,
    strip_title_decorators,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPORTIFY_HEADER = (
    '"Track URI","Track Name","Album Name","Artist Name(s)",'
    '"Release Date","Duration (ms)","Popularity",'
    '"Added By","Added At","Genres","Label","ISRC",'
    '"Loudness","Tempo","Position"\n'
)


def _make_csv(rows: list[str]) -> str:
    return EXPORTIFY_HEADER + "\n".join(rows)


def _write_tmp_csv(content: str) -> str:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", encoding="utf-8", delete=False
    )
    f.write(content)
    f.close()
    return f.name


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def test_detect_json_mode():
    content = json.dumps([{"source": "qobuz", "media_type": "track", "id": "123"}])
    assert detect_file_mode(content) == "json"


def test_detect_exportify_csv_mode():
    content = _make_csv(
        [
            '"spotify:track:abc","Song","Album","Artist","2022-01-01",180000,50,"user","2022","","","ISRC123","","","1"'
        ]
    )
    assert detect_file_mode(content) == "exportify-csv"


def test_detect_url_mode():
    content = "https://open.qobuz.com/track/12345\nhttps://www.deezer.com/track/67890\n"
    assert detect_file_mode(content) == "urls"


def test_detect_mode_strips_bom():
    content = "\ufeff" + _make_csv([])
    assert detect_file_mode(content) == "exportify-csv"


def test_detect_mode_empty_falls_back_to_urls():
    assert detect_file_mode("") == "urls"


def test_detect_mode_json_list_only():
    # A JSON object (not list) should NOT be treated as json mode
    content = '{"source": "qobuz"}'
    assert detect_file_mode(content) != "json"


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def test_parse_basic_row():
    content = _make_csv(
        [
            '"spotify:track:abc","Blue in Green","Kind of Blue","Miles Davis","1959-03-02",337800,85,"","","Jazz","","USBN41601500","-15.2","120.3","1"'
        ]
    )
    path = _write_tmp_csv(content)
    try:
        playlist_name, rows = parse_exportify_csv(path)
        assert playlist_name == os.path.basename(path)[:-4]  # stem without .csv
        assert len(rows) == 1
        row = rows[0]
        assert row.track_name == "Blue in Green"
        assert row.album == "Kind of Blue"
        assert row.artists_raw == "Miles Davis"
        assert row.artists_list == ["Miles Davis"]
        assert row.release_date == "1959-03-02"
        assert row.isrc == "USBN41601500"
        assert row.spotify_uri == "spotify:track:abc"
        assert row.genres == "Jazz"
        assert row.loudness == "-15.2"
        assert row.tempo == "120.3"
        assert row.position == 1
        assert row.row_index == 0
        assert row.source_row_index == 0
        assert row.duration_ms == 337800
        assert row.canonical_track_name == "Blue in Green"
    finally:
        os.unlink(path)


def test_parse_semicolon_artists():
    content = _make_csv(
        [
            '"spotify:track:xyz","Song","Album","Artist A;Artist B;Artist C","2020","180000","70","","","","","","","","1"'
        ]
    )
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        assert rows[0].artists_list == ["Artist A", "Artist B", "Artist C"]
        assert rows[0].artists_raw == "Artist A;Artist B;Artist C"
    finally:
        os.unlink(path)


def test_parse_utf8_bom():
    # Write file with BOM
    f = tempfile.NamedTemporaryFile(mode="wb", suffix=".csv", delete=False)
    data = (
        EXPORTIFY_HEADER
        + '"spotify:track:bom","BOM Track","BOM Album","Artist","2023","","50","","","","","","","","1"\n'
    ).encode("utf-8-sig")
    f.write(data)
    f.close()
    try:
        _, rows = parse_exportify_csv(f.name)
        assert rows[0].track_name == "BOM Track"
    finally:
        os.unlink(f.name)


def test_parse_optional_columns_missing():
    # Minimal header without optional columns
    minimal_header = '"Track URI","Track Name","Artist Name(s)"\n'
    content = minimal_header + '"spotify:track:min","Minimal","Artist"\n'
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        assert rows[0].track_name == "Minimal"
        assert rows[0].isrc == ""
        assert rows[0].genres == ""
        assert rows[0].loudness == ""
        assert rows[0].tempo == ""
        assert rows[0].album == ""
    finally:
        os.unlink(path)


def test_parse_unknown_columns_ignored():
    header = '"Track URI","Track Name","Artist Name(s)","UNKNOWN_COL_XYZ"\n'
    content = header + '"uri","Song","Artist","ignore_this"\n'
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        assert rows[0].track_name == "Song"
    finally:
        os.unlink(path)


def test_parse_position_from_column():
    content = _make_csv(
        [
            '"spotify:track:a","Song1","A","ArtistA","2021","","50","","","","","","","","5"',
            '"spotify:track:b","Song2","B","ArtistB","2021","","50","","","","","","","","10"',
        ]
    )
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        assert rows[0].position == 5
        assert rows[1].position == 10
    finally:
        os.unlink(path)


def test_parse_multiple_rows():
    rows_data = [
        '"spotify:track:1","Track One","Album A","Artist1","2010","","60","","","","","ISRC1","","","1"',
        '"spotify:track:2","Track Two","Album B","Artist2","2011","","70","","","","","ISRC2","","","2"',
        '"spotify:track:3","Track Three","Album C","Artist3","2012","","80","","","","","ISRC3","","","3"',
    ]
    content = _make_csv(rows_data)
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        assert len(rows) == 3
        assert [r.track_name for r in rows] == ["Track One", "Track Two", "Track Three"]
        assert [r.row_index for r in rows] == [0, 1, 2]
    finally:
        os.unlink(path)


def test_parse_empty_csv():
    content = EXPORTIFY_HEADER
    path = _write_tmp_csv(content)
    try:
        playlist_name, rows = parse_exportify_csv(path)
        assert rows == []
    finally:
        os.unlink(path)


def test_parse_isrc_track_isrc_fallback():
    """Test that 'Track ISRC' column is used when 'ISRC' is absent."""
    header = '"Track URI","Track Name","Artist Name(s)","Track ISRC"\n'
    content = header + '"uri","Song","Artist","ISRC999"\n'
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        assert rows[0].isrc == "ISRC999"
    finally:
        os.unlink(path)


def test_parse_exportify_csv_does_not_create_backup_file():
    content = _make_csv(
        [
            '"spotify:track:2","Track B","Album","Zulu","2020","","","","","","","","","","2"',
            '"spotify:track:1","Track A","Album","Alpha","2020","","","","","","","","","","1"',
        ]
    )
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        assert [r.artists_raw for r in rows] == ["Zulu", "Alpha"]
        backup = Path(path).with_name(f"{Path(path).stem}.original{Path(path).suffix}")
        assert not backup.exists()
    finally:
        os.unlink(path)


def test_partition_exportify_rows_artist_batched_orders_in_memory():
    content = _make_csv(
        [
            '"spotify:track:2","Track B","Album","Zulu","2020","","","","","","","","","","2"',
            '"spotify:track:1","Track A","Album","Alpha","2020","","","","","","","","","","1"',
            '"spotify:track:3","Track C","Album","Alpha","2020","","","","","","","","","","3"',
        ]
    )
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        batches = partition_exportify_rows_artist_batched(rows, max_batch_size=10)
        ordered_artists = [row.artists_raw for batch in batches for row in batch]
        assert ordered_artists == ["Alpha", "Alpha", "Zulu"]
    finally:
        os.unlink(path)


def test_parse_exportify_csv_ignores_extra_fields():
    # Malformed row has one extra comma-delimited field at the end; parser should still read it.
    content = _make_csv(
        [
            '"spotify:track:2","Track B","Album","Zulu","2020","","","","","","","","","","2",EXTRA',
            '"spotify:track:1","Track A","Album","Alpha","2020","","","","","","","","","","1"',
        ]
    )
    path = _write_tmp_csv(content)
    try:
        _, rows = parse_exportify_csv(path)
        assert [r.artists_raw for r in rows] == ["Zulu", "Alpha"]
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _make_row(
    title: str = "Blue in Green",
    artists: list[str] | None = None,
    album: str = "Kind of Blue",
    date: str = "1959",
    isrc: str = "",
    duration_ms: int | None = None,
) -> ExportifyCsvRow:
    artists = artists or ["Miles Davis"]
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
        position=1,
        row_index=0,
        duration_ms=duration_ms,
    )


def test_score_exact_isrc_wins():
    row = _make_row(isrc="USBN41601500")
    score = score_candidate(
        row, "Completely Different Title", "Wrong Artist", "", "", "USBN41601500"
    )
    assert score == 100


def test_score_exact_title_artist():
    row = _make_row()
    score = score_candidate(
        row, "Blue in Green", "Miles Davis", "Kind of Blue", "1959", ""
    )
    assert score >= 60


def test_score_title_album_bonus():
    row = _make_row()
    score = score_candidate(
        row, "Blue in Green", "Unknown Artist", "Kind of Blue", "1959", ""
    )
    assert 40 <= score < 70


def test_score_year_bonus():
    row = _make_row(date="1959")
    score_with_year = score_candidate(
        row, "Blue in Green", "Miles Davis", "", "1959", ""
    )
    score_without_year = score_candidate(
        row, "Blue in Green", "Miles Davis", "", "2010", ""
    )
    assert score_with_year > score_without_year


def test_score_no_match():
    row = _make_row()
    score = score_candidate(
        row, "Completely Different Song", "Other Artist", "Other Album", "2022", ""
    )
    assert score == 0


def test_score_case_insensitive():
    row = _make_row()
    score = score_candidate(row, "BLUE IN GREEN", "MILES DAVIS", "", "1959", "")
    assert score >= 60


def test_score_variant_title_normalization():
    row = _make_row(title="Song Name")
    score = score_candidate(
        row,
        "Song Name (2011 Remaster)",
        "Miles Davis",
        "Kind of Blue",
        "1959",
        "",
    )
    assert score >= 55


def test_normalise_variant_text_removes_all_variant_markers():
    assert (
        _normalise_variant_text(
            "Song Name Bonus Track Live Radio Edit ft Artist (2011 Remaster)"
        )
        == "song name artist"
    )


def test_strip_title_decorators_removes_feat_and_remaster_suffixes():
    assert strip_title_decorators("1, 2 Step (feat. Missy Elliott)") == "1, 2 Step"
    assert strip_title_decorators("Song Name - 2011 Remaster") == "Song Name"
    assert strip_title_decorators("Song Name \u2013 2011 Remaster") == "Song Name"
    assert (
        strip_title_decorators("Layla - Acoustic; Live at MTV Unplugged; 2013 Remaster")
        == "Layla"
    )
    assert (
        strip_title_decorators("Bug Powder Dust - Kruder & Dorfmeister Session")
        == "Bug Powder Dust - Kruder & Dorfmeister Session"
    )


def test_score_featured_title_matches_plain_canonical_title():
    row = _make_row(
        title="1, 2 Step (feat. Missy Elliott)",
        artists=["Ciara", "Missy Elliott"],
        album="Goodies",
        date="2004",
    )
    score = score_candidate(
        row,
        "1, 2 Step",
        "Ciara",
        "Goodies",
        "2004",
        "",
    )
    assert score >= 50


def test_score_variant_title_does_not_match_empty_normalized_titles():
    row = _make_row(title="1999")
    score = score_candidate(
        row,
        "2000",
        "Miles Davis",
        "Kind of Blue",
        "1959",
        "",
    )
    assert score == 0


def test_is_usable_exportify_row_requires_track_and_artist():
    assert is_usable_exportify_row(_make_row())
    assert not is_usable_exportify_row(_make_row(title=""))
    blank_artist_row = ExportifyCsvRow(
        track_name="Song",
        artists_raw="",
        artists_list=[],
        album="Album",
        release_date="2020",
        isrc="",
        spotify_uri="",
        genres="",
        loudness="",
        tempo="",
        position=1,
        row_index=0,
    )
    assert not is_usable_exportify_row(blank_artist_row)


def test_partition_exportify_rows_artist_batched_allows_artist_group_overflow():
    rows = [
        _make_row(title=f"S{i}", artists=["Artist A"], album="", date="", isrc="")
        for i in range(37)
    ] + [
        _make_row(title=f"T{i}", artists=["Artist B"], album="", date="", isrc="")
        for i in range(12)
    ]
    batches = partition_exportify_rows_artist_batched(rows, max_batch_size=40)
    assert [len(b) for b in batches] == [49]


def test_partition_exportify_rows_artist_batched_large_single_artist_group():
    rows = [
        _make_row(title=f"S{i}", artists=["Artist A"], album="", date="", isrc="")
        for i in range(78)
    ]
    batches = partition_exportify_rows_artist_batched(rows, max_batch_size=40)
    assert len(batches) == 1
    assert len(batches[0]) == 78


def test_exportify_artist_group_key_is_deterministic():
    row = _make_row(artists=["Ciara"])
    assert exportify_artist_group_key(row) == "ciara"


def test_score_multi_artist_prefers_full_coverage():
    row = _make_row(artists=["Artist A", "Artist B"])
    full = score_candidate(
        row, "Blue in Green", "Artist A, Artist B", "Kind of Blue", "1959", ""
    )
    partial = score_candidate(
        row, "Blue in Green", "Artist A", "Kind of Blue", "1959", ""
    )
    assert full > partial


def test_normalise():
    assert _normalise("  Hello  World  ") == "hello world"
    assert _normalise("Hello-World") == "hello world"
    assert _normalise("Hello's World!") == "hellos world"


def test_artist_overlap_basic():
    assert _artist_overlap(["Miles Davis"], "Miles Davis")
    assert not _artist_overlap(["Miles Davis"], "John Coltrane")
    assert _artist_overlap(["Artist A", "Artist B"], "Artist B")


# ---------------------------------------------------------------------------
# score_candidate_repair — fuzzy matching
# ---------------------------------------------------------------------------


def test_repair_score_exact_delegates_to_standard():
    """When standard scorer returns > 0, repair scorer returns the same value."""
    from streamrip.file_lists import score_candidate_repair

    row = _make_row()
    std = score_candidate(row, "Blue in Green", "Miles Davis", "", "", "")
    repair = score_candidate_repair(row, "Blue in Green", "Miles Davis", "", "", "")
    assert repair == std
    assert repair >= 60


def test_repair_score_isrc_short_circuits():
    from streamrip.file_lists import score_candidate_repair

    row = _make_row(isrc="USJAZ1234567")
    score = score_candidate_repair(
        row, "Totally Different Title", "Unknown Artist", "", "", "USJAZ1234567"
    )
    assert score == 100


def test_repair_score_fuzzy_title_with_artist():
    """Fuzzy title match + artist overlap should score 35."""
    from streamrip.file_lists import score_candidate_repair

    row = _make_row()
    # Slightly misspelled title that still exceeds 0.80 ratio
    score = score_candidate_repair(row, "Blue in Gren", "Miles Davis", "", "", "")
    assert score == 35


def test_repair_score_fuzzy_title_with_album():
    """Fuzzy title match + album partial match should score 28."""
    from streamrip.file_lists import score_candidate_repair

    row = _make_row()
    score = score_candidate_repair(
        row, "Blue in Gren", "Unknown Artist", "Kind of Blue", "", ""
    )
    assert score == 28


def test_repair_score_fuzzy_title_only():
    """Fuzzy title match only should score 20."""
    from streamrip.file_lists import score_candidate_repair

    row = _make_row()
    score = score_candidate_repair(
        row, "Blue in Gren", "Unknown Artist", "Other Album", "", ""
    )
    assert score == 20


def test_repair_score_fuzzy_with_year_bonus():
    """Fuzzy score should get +5 year bonus when years match."""
    from streamrip.file_lists import score_candidate_repair

    row = _make_row()
    score_with_year = score_candidate_repair(
        row, "Blue in Gren", "Miles Davis", "", "1959-01-01", ""
    )
    score_without_year = score_candidate_repair(
        row, "Blue in Gren", "Miles Davis", "", "2020-01-01", ""
    )
    assert score_with_year == score_without_year + 5


def test_repair_score_very_different_title_returns_zero():
    """A completely different title should not match even in repair mode."""
    from streamrip.file_lists import score_candidate_repair

    row = _make_row()
    score = score_candidate_repair(row, "Bohemian Rhapsody", "Queen", "", "", "")
    assert score == 0


def test_repair_score_empty_title_returns_zero():
    from streamrip.file_lists import score_candidate_repair

    row = _make_row()
    score = score_candidate_repair(row, "", "Miles Davis", "", "", "")
    assert score == 0


def test_source_declared_instrumental_is_honored():
    row = _make_row(title="My Song (Instrumental)", artists=["Artist A"], album="Album")
    instrumental = score_candidate(
        row, "My Song - Instrumental", "Artist A", "Album", "2020", ""
    )
    studio = score_candidate(row, "My Song", "Artist A", "Album", "2020", "")
    assert instrumental > studio


def test_unexpected_instrumental_is_rejected_by_default():
    row = _make_row(title="My Song", artists=["Artist A"], album="Album")
    score = score_candidate(row, "My Song (Instrumental)", "Artist A", "Album", "", "")
    assert score == 0


def test_unexpected_instrumental_can_be_allowed_by_policy():
    row = _make_row(title="My Song", artists=["Artist A"], album="Album")
    policy = MatchPolicy(instrumental_mode="penalty")
    score = score_candidate(
        row,
        "My Song (Instrumental)",
        "Artist A",
        "Album",
        "",
        "",
        policy=policy,
    )
    assert score > 0


def test_remaster_equivalent_default_accepts_with_year_deemphasis():
    row = _make_row(title="My Song", artists=["Artist A"], date="1991")
    remaster = score_candidate(
        row, "My Song (2019 Remaster)", "Artist A", "Any Album", "2019", ""
    )
    plain = score_candidate(row, "My Song", "Artist A", "Any Album", "1991", "")
    assert remaster > 0
    assert abs(remaster - plain) <= 20


def test_radio_edit_policy_controls_acceptance():
    row = _make_row(title="My Song", artists=["Artist A"])
    reject_policy = MatchPolicy(radio_edit_mode="reject")
    penalty_policy = MatchPolicy(radio_edit_mode="penalty")
    assert (
        score_candidate(
            row,
            "My Song - Radio Edit",
            "Artist A",
            "",
            "",
            "",
            policy=reject_policy,
        )
        == 0
    )
    assert (
        score_candidate(
            row,
            "My Song - Radio Edit",
            "Artist A",
            "",
            "",
            "",
            policy=penalty_policy,
        )
        > 0
    )


def test_album_deemphasis_prefers_identity_and_duration():
    row = _make_row(title="Blue in Green", artists=["Miles Davis"], duration_ms=340000)
    better_identity = score_candidate(
        row, "Blue in Green", "Miles Davis", "Compilation X", "1959", "", 340500
    )
    weaker_identity = score_candidate(
        row, "Blue in Green", "Unknown", "Kind of Blue", "1959", "", 340500
    )
    assert better_identity > weaker_identity


def test_duration_reranking_penalizes_wrong_length():
    row = _make_row(title="Track", artists=["Artist A"], duration_ms=210000)
    close = score_candidate(row, "Track", "Artist A", "", "", "", 211000)
    far = score_candidate(row, "Track", "Artist A", "", "", "", 258000)
    assert close > far


def test_bad_context_release_rejected():
    row = _make_row(title="Track", artists=["Artist A"])
    score = score_candidate(
        row, "Track", "Artist A", "Karaoke Tribute Collection", "", ""
    )
    assert score == 0


def test_no_false_positive_for_wrong_title_with_same_artist():
    row = _make_row(title="Blue in Green", artists=["Miles Davis"])
    score = score_candidate(row, "So What", "Miles Davis", "Kind of Blue", "1959", "")
    assert score == 0
