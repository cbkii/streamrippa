#!/usr/bin/env python3
"""Tighten normal-mode artist identity after benchmark validation."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def main() -> None:
    runner = ROOT / "tools/_apply_resolver_overhaul.py"
    text = runner.read_text()
    old = '''    else:
        weak_context = False
        if row.album and candidate_album and (
            core_title_match or neutral_extension or guarded_fuzzy_match
        ):
            weak_context = album_ok
        if not weak_context:
            return CandidateExplanation(
                score=0, reason_codes=("reject_artist_mismatch",), signals=signals
            )
        score -= 10
        reasons.append("penalty_weak_artist_context")
'''
    new = '''    else:
        # Album/title/duration agreement is corroboration, never a substitute
        # for artist identity in normal matching. This prevents common-title
        # collisions and compilation metadata from promoting unrelated artists.
        return CandidateExplanation(
            score=0, reason_codes=("reject_artist_mismatch",), signals=signals
        )
'''
    runner.write_text(replace_once(text, old, new, "artist hard gate"))

    tests = ROOT / "tests/test_file_lists.py"
    text = tests.read_text()
    old = '''def test_score_title_album_bonus():
    row = _make_row()
    score = score_candidate(
        row, "Blue in Green", "Unknown Artist", "Kind of Blue", "1959", ""
    )
    assert 40 <= score < 70
'''
    new = '''def test_score_title_album_does_not_override_artist_mismatch():
    row = _make_row()
    score = score_candidate(
        row, "Blue in Green", "Unknown Artist", "Kind of Blue", "1959", ""
    )
    assert score == 0
'''
    tests.write_text(replace_once(text, old, new, "album cannot replace artist test"))
    print("artist identity guard tightened")


if __name__ == "__main__":
    main()
