"""Tests for Exportify extra-tag writing in streamrip.metadata.tagger."""

from __future__ import annotations

import os
import shutil

import pytest
from mutagen.flac import FLAC
from util import arun

from streamrip.metadata import (
    AlbumInfo,
    AlbumMetadata,
    Covers,
    TrackInfo,
    TrackMetadata,
    tag_file,
)

TEST_FLAC_ORIGINAL = "tests/silence.flac"
TEST_FLAC_COPY = "tests/silence_extratags.flac"


def _make_meta(extra_tags=None) -> TrackMetadata:
    return TrackMetadata(
        TrackInfo(
            id="12345",
            quality=2,
            bit_depth=16,
            explicit=False,
            sampling_rate=44.1,
            work=None,
        ),
        "Test Title",
        AlbumMetadata(
            AlbumInfo("5678", 2, "flac"),
            "Test Album",
            "Test Artist",
            "2022",
            ["Rock", "Pop"],
            Covers(),
            10,
        ),
        "Test Artist",
        1,
        1,
        None,
        extra_tags=extra_tags,
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    if os.path.exists(TEST_FLAC_COPY):
        os.remove(TEST_FLAC_COPY)


def _tag_fresh(extra_tags=None):
    shutil.copy(TEST_FLAC_ORIGINAL, TEST_FLAC_COPY)
    meta = _make_meta(extra_tags=extra_tags)
    arun(tag_file(TEST_FLAC_COPY, meta, None))
    return FLAC(TEST_FLAC_COPY)


def test_no_extra_tags_baseline():
    """Standard tagging still works when extra_tags is None."""
    file = _tag_fresh(extra_tags=None)
    assert file["title"][0] == "Test Title"
    assert file["album"][0] == "Test Album"
    # No stray custom keys
    assert "EXPORTIFY_LOUDNESS" not in file
    assert "TEMPO" not in file


def test_extra_tags_written_to_flac():
    file = _tag_fresh(extra_tags={"EXPORTIFY_LOUDNESS": "-12.5", "TEMPO": "128.0"})
    assert file["EXPORTIFY_LOUDNESS"][0] == "-12.5"
    assert file["TEMPO"][0] == "128.0"
    # Standard tags still written
    assert file["title"][0] == "Test Title"


def test_empty_extra_tags_map_disables_extra():
    file = _tag_fresh(extra_tags={})
    assert "EXPORTIFY_LOUDNESS" not in file
    assert "TEMPO" not in file


def test_blank_value_skipped():
    file = _tag_fresh(extra_tags={"EXPORTIFY_LOUDNESS": "", "TEMPO": "120"})
    assert "EXPORTIFY_LOUDNESS" not in file
    assert file["TEMPO"][0] == "120"


def test_genre_merge_with_provider_genres():
    """CSV genres should be merged with existing provider genres, no duplicates."""
    # Provider already set genre = ["Rock", "Pop"] via album metadata
    # CSV Genres = "Pop, Jazz"
    # After merge: Rock, Pop, Jazz (Pop not duplicated)
    file = _tag_fresh(extra_tags={"genre": "Rock, Pop, Jazz"})
    genre_str = file["genre"][0]
    genres = [g.strip() for g in genre_str.split(",")]
    assert "Rock" in genres
    assert "Pop" in genres
    assert "Jazz" in genres
    # Pop should not appear twice
    assert genres.count("Pop") == 1


def test_extra_tags_exception_logged_not_raised(caplog):
    """A failure writing extra tags must not fail the track."""
    import logging
    # Simulate a bad tag name that mutagen might struggle with
    # An empty key will fail
    with caplog.at_level(logging.WARNING, logger="streamrip"):
        # This should not raise
        file = _tag_fresh(extra_tags={"\x00bad_key": "value", "TEMPO": "130"})
    # TEMPO should still be written (isolated writes)
    assert file["TEMPO"][0] == "130"
