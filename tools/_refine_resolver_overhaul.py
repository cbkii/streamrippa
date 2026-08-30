#!/usr/bin/env python3
"""Refine the staged resolver overhaul after compatibility validation."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def patch_overhaul_runner() -> None:
    path = ROOT / "tools/_apply_resolver_overhaul.py"
    text = path.read_text()

    # Keep the established strategy names where the semantic query remains the
    # same.  This preserves unresolved-log/telemetry compatibility while adding
    # the new alternate and broad strategies alongside them.
    replacements = {
        '"title-artist-album",': '"structured",',
        '"canonical-title-artist-album",': '"stripped-structured",',
        '("title-artist", " ".join(p for p in (row.track_name, first_artist) if p))': '("generic", " ".join(p for p in (row.track_name, first_artist) if p))',
        '"canonical-title-artist",': '"stripped-generic",',
    }
    for old, new in replacements.items():
        text = replace_once(text, old, new, f"strategy rename {old}")

    old_hint = '''                    if hinted_candidate.score >= provider_min_score:
                        _record_hit(hinted_id, "id-hint", hinted_candidate)
                        if (
                            hinted_candidate.score == 100
                            and "accepted_isrc_match" in hinted_candidate.reason_codes
                        ):
                            return ResolverOutcome(
                                candidate=hinted_candidate,
                                reason="matched",
                                query=hinted_id,
                                strategy="id-hint",
                                rejected=closest_rejected,
                                attempts=tuple(attempts),
                            )
'''
    new_hint = '''                    if hinted_candidate.score >= provider_min_score:
                        # A repair candidate ID is a deterministic provider-track
                        # identity captured by a previous resolver pass.  Once its
                        # current metadata still clears the strict scorer, honour
                        # it as the repair fast path instead of spending new search
                        # calls trying to beat an already validated ID.
                        return ResolverOutcome(
                            candidate=hinted_candidate,
                            reason="matched",
                            query=hinted_id,
                            strategy="id-hint",
                            rejected=closest_rejected,
                            attempts=tuple(attempts),
                        )
'''
    text = replace_once(text, old_hint, new_hint, "repair ID fast path")

    # The normal plan already includes title-only discovery.  A second expanded
    # pass is useful for genuine no-result/low-confidence outcomes, but provider
    # errors/disabled sessions and explicit safety rejections must not trigger a
    # duplicate request storm and exponential provider cooldowns.
    anchor = '''    text = replace_between(
        text,
        "        async def _resolve_for_client(\\n",
        "        primary_outcome = await _resolve_for_client(\\n",
        new_resolver,
        label="client resolver",
    )

'''
    bounded_escalation = anchor + '''    old_escalation = """        primary_outcome = await _resolve_for_client(
            self.primary_client, escalation=False
        )

        # Search fallback service if configured
        if self.fallback_client is not None and not (
            primary_outcome.candidate is not None
            and primary_outcome.candidate.score >= primary_min_score
        ):
            fallback_outcome = await _resolve_for_client(
                self.fallback_client, escalation=False
            )
        if (
            primary_outcome.candidate is None
            or primary_outcome.candidate.score < primary_min_score
        ):
            primary_outcome = await _resolve_for_client(
                self.primary_client, escalation=True
            )
        if (
            self.fallback_client is not None
            and (
                fallback_outcome.candidate is None
                or fallback_outcome.candidate.score < fallback_min_score
            )
            and (
                primary_outcome.candidate is None
                or primary_outcome.candidate.score < primary_min_score
            )
        ):
            fallback_outcome = await _resolve_for_client(
                self.fallback_client, escalation=True
            )
"""
    new_escalation = """        primary_outcome = await _resolve_for_client(
            self.primary_client, escalation=False
        )

        def _eligible_for_escalation(outcome: ResolverOutcome) -> bool:
            return outcome.reason == REASON_NO_RESULTS or outcome.reason == REASON_NO_RESULTS_AFTER_BROAD or outcome.reason.startswith(REASON_LOW_CONFIDENCE)

        # Search fallback service if configured and primary did not strictly match.
        if self.fallback_client is not None and primary_outcome.reason != "matched":
            fallback_outcome = await _resolve_for_client(
                self.fallback_client, escalation=False
            )

        # Only repeat with the expanded result window for genuine discovery
        # misses. Provider errors and explicit safety rejections are terminal for
        # this row/provider and must not be amplified into repeated calls.
        if _eligible_for_escalation(primary_outcome):
            primary_outcome = await _resolve_for_client(
                self.primary_client, escalation=True
            )
        if (
            self.fallback_client is not None
            and fallback_outcome.reason != "matched"
            and _eligible_for_escalation(fallback_outcome)
            and primary_outcome.reason != "matched"
        ):
            fallback_outcome = await _resolve_for_client(
                self.fallback_client, escalation=True
            )
"""
    text = replace_once(
        text,
        old_escalation,
        new_escalation,
        label="bounded escalation",
    )

'''
    text = replace_once(text, anchor, bounded_escalation, "inject bounded escalation")
    path.write_text(text)


def patch_existing_tests() -> None:
    file_lists = ROOT / "tests/test_file_lists.py"
    text = file_lists.read_text()
    text = replace_once(
        text,
        '''def test_score_exact_isrc_wins():
    row = _make_row(isrc="USBN41601500")
    score = score_candidate(
        row, "Completely Different Title", "Wrong Artist", "", "", "USBN41601500"
    )
    assert score == 100
''',
        '''def test_score_exact_isrc_wins_when_identity_context_is_safe():
    row = _make_row(isrc="USBN41601500")
    score = score_candidate(
        row,
        "Blue in Green",
        "Miles Davis",
        "Later Compilation",
        "2018",
        "USBN41601500",
    )
    assert score == 100
''',
        "standard ISRC compatibility test",
    )
    text = replace_once(
        text,
        '''def test_repair_score_isrc_short_circuits():
    from streamrip.file_lists import score_candidate_repair

    row = _make_row(isrc="USJAZ1234567")
    score = score_candidate_repair(
        row, "Totally Different Title", "Unknown Artist", "", "", "USJAZ1234567"
    )
    assert score == 100
''',
        '''def test_repair_score_isrc_short_circuits_when_identity_context_is_safe():
    from streamrip.file_lists import score_candidate_repair

    row = _make_row(isrc="USJAZ1234567")
    score = score_candidate_repair(
        row,
        "Blue in Green",
        "Miles Davis",
        "Later Compilation",
        "2018",
        "USJAZ1234567",
    )
    assert score == 100
''',
        "repair ISRC compatibility test",
    )
    file_lists.write_text(text)

    playlist = ROOT / "tests/test_csv_playlist.py"
    text = playlist.read_text()
    old_pick = '''def test_pick_best_candidate_isrc_wins():
    row = _make_row(isrc="ISRC001")
    client = _make_client("deezer")
    pages = [
        {
            "data": [
                {
                    "id": 1,
                    "title": "Wrong Song",
                    "artist": {"name": "X"},
                    "isrc": "ISRC001",
                    "album": {"title": ""},
                },
                {
                    "id": 2,
                    "title": "Song",
                    "artist": {"name": "Artist"},
                    "isrc": "OTHER",
                    "album": {"title": "Album"},
                },
            ]
        }
    ]
    cand = _pick_best_candidate(row, "deezer", pages, client)
    assert cand is not None
    assert cand.id == "1"
    assert cand.score == 100
'''
    new_pick = '''def test_pick_best_candidate_isrc_wins_when_context_is_safe():
    row = _make_row(isrc="ISRC001")
    client = _make_client("deezer")
    pages = [
        {
            "data": [
                {
                    "id": 1,
                    "title": "Song",
                    "artist": {"name": "Artist"},
                    "isrc": "ISRC001",
                    "album": {"title": "Later Compilation"},
                },
                {
                    "id": 2,
                    "title": "Song",
                    "artist": {"name": "Artist"},
                    "isrc": "OTHER",
                    "album": {"title": "Album"},
                },
            ]
        }
    ]
    cand = _pick_best_candidate(row, "deezer", pages, client)
    assert cand is not None
    assert cand.id == "1"
    assert cand.score == 100
'''
    text = replace_once(text, old_pick, new_pick, "safe ISRC picker test")
    text = replace_once(
        text,
        '    assert ("low confidence" in content) or ("no results" in content)\n',
        '    assert "variant-conflict" in content\n',
        "precise variant diagnostic test",
    )
    text = replace_once(
        text,
        '    assert "search failure" in content\n',
        '    assert "provider-search-error" in content\n',
        "precise provider error diagnostic test",
    )
    playlist.write_text(text)

    overhaul_tests = ROOT / "tests/test_csv_resolver_overhaul.py"
    text = overhaul_tests.read_text()
    text = text.replace('assert "title-artist-album" in strategies', 'assert "structured" in strategies')
    text = text.replace('assert "title-artist" in strategies', 'assert "generic" in strategies')
    text = text.replace('query_map["title-artist-album"]', 'query_map["structured"]')
    text = text.replace('strategies.index("title-artist")', 'strategies.index("generic")')
    overhaul_tests.write_text(text)


def main() -> None:
    patch_overhaul_runner()
    patch_existing_tests()
    print("resolver refinement applied")


if __name__ == "__main__":
    main()
