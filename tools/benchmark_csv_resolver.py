#!/usr/bin/env python3
"""Deterministic benchmark for CSV resolver matching heuristics.

This intentionally avoids live provider calls so it is stable in CI.  It can
also inspect a real Exportify CSV and report how much identity metadata is
available for resolver decisions.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from streamrip.file_lists import (
    ExportifyCsvRow,
    MatchPolicy,
    explain_candidate_score,
    parse_exportify_csv,
)


@dataclass(frozen=True)
class Case:
    name: str
    row: ExportifyCsvRow
    candidate_title: str
    candidate_artist: str
    candidate_album: str
    candidate_date: str
    candidate_isrc: str
    candidate_duration_ms: int | None
    expect_accept: bool


def row(
    title: str,
    artist: str,
    *,
    album: str = "Album",
    date: str = "2005-01-01",
    isrc: str = "",
    duration_ms: int | None = 240000,
) -> ExportifyCsvRow:
    return ExportifyCsvRow(
        track_name=title,
        artists_raw=artist,
        artists_list=[artist],
        album=album,
        release_date=date,
        isrc=isrc,
        spotify_uri="spotify:track:benchmark",
        genres="",
        loudness="",
        tempo="",
        position=1,
        row_index=0,
        source_row_index=0,
        canonical_track_name=title,
        duration_ms=duration_ms,
        repair_candidate_ids=None,
    )


def fixture_cases() -> list[Case]:
    hard_twelve = row(
        "Hard Twelve",
        "Beat Assailant",
        album="Imperial Pressure",
        duration_ms=239000,
    )
    return [
        Case(
            "exact",
            row("Ordinary Song", "Artist", duration_ms=200000),
            "Ordinary Song",
            "Artist",
            "Album",
            "2005-01-01",
            "",
            200500,
            True,
        ),
        Case(
            "neutral-parenthetical-subtitle",
            hard_twelve,
            "Hard Twelve (The Ante)",
            "Beat Assailant",
            "Imperial Pressure",
            "2005-08-22",
            "",
            239500,
            True,
        ),
        Case(
            "neutral-dash-subtitle",
            hard_twelve,
            "Hard Twelve - The Ante",
            "Beat Assailant",
            "Imperial Pressure",
            "2005-08-22",
            "",
            239500,
            True,
        ),
        Case(
            "wrong-artist",
            hard_twelve,
            "Hard Twelve",
            "Different Artist",
            "Imperial Pressure",
            "2005-08-22",
            "",
            239500,
            False,
        ),
        Case(
            "live-substitution",
            hard_twelve,
            "Hard Twelve (Live)",
            "Beat Assailant",
            "Imperial Pressure",
            "2005-08-22",
            "",
            239500,
            False,
        ),
        Case(
            "karaoke-substitution",
            hard_twelve,
            "Hard Twelve",
            "Beat Assailant",
            "Hard Twelve Karaoke Tribute",
            "2005-08-22",
            "",
            239500,
            False,
        ),
        Case(
            "isrc-reissue",
            row(
                "Hard Twelve",
                "Beat Assailant",
                album="Original Album",
                isrc="FRABC0512345",
                duration_ms=239000,
            ),
            "Hard Twelve",
            "Beat Assailant",
            "Later Compilation",
            "2018-01-01",
            "FRABC0512345",
            239500,
            True,
        ),
        Case(
            "isrc-duration-conflict",
            row(
                "Hard Twelve",
                "Beat Assailant",
                isrc="FRABC0512345",
                duration_ms=239000,
            ),
            "Hard Twelve",
            "Beat Assailant",
            "Album",
            "2005-01-01",
            "FRABC0512345",
            290000,
            False,
        ),
    ]


def run_fixtures() -> int:
    policy = MatchPolicy(enable_guarded_fuzzy_normal=True)
    cases = fixture_cases()
    correct = 0
    accepted = 0
    rejected = 0
    reasons: dict[str, int] = {}

    for case in cases:
        result = explain_candidate_score(
            case.row,
            case.candidate_title,
            case.candidate_artist,
            case.candidate_album,
            case.candidate_date,
            case.candidate_isrc,
            case.candidate_duration_ms,
            policy=policy,
        )
        actual_accept = result.score >= 50
        accepted += int(actual_accept)
        rejected += int(not actual_accept)
        correct += int(actual_accept == case.expect_accept)
        for reason in result.reason_codes or ("accepted-scored",):
            reasons[reason] = reasons.get(reason, 0) + 1
        state = "PASS" if actual_accept == case.expect_accept else "FAIL"
        print(
            f"{state:4} {case.name:30} score={result.score:3} "
            f"expected={'accept' if case.expect_accept else 'reject'} "
            f"reasons={','.join(result.reason_codes) or '-'}"
        )

    print("\nfixture benchmark")
    print(f"  total:     {len(cases)}")
    print(f"  correct:   {correct}")
    print(f"  accepted:  {accepted}")
    print(f"  rejected:  {rejected}")
    print(f"  accuracy:  {correct / len(cases) * 100:.1f}%")
    print("  reasons:")
    for reason, count in sorted(reasons.items()):
        print(f"    {reason}: {count}")
    return 0 if correct == len(cases) else 1


def inspect_csv(path: Path) -> None:
    _, rows = parse_exportify_csv(str(path))
    total = len(rows)
    with_isrc = sum(bool(r.isrc) for r in rows)
    with_duration = sum(bool(r.duration_ms) for r in rows)
    with_album = sum(bool(r.album) for r in rows)
    with_date = sum(bool(r.release_date) for r in rows)

    def pct(value: int) -> str:
        return f"{(value / total * 100) if total else 0:.1f}%"

    print("\nCSV identity coverage")
    print(f"  rows:       {total}")
    print(f"  ISRC:       {with_isrc} ({pct(with_isrc)})")
    print(f"  duration:   {with_duration} ({pct(with_duration)})")
    print(f"  album:      {with_album} ({pct(with_album)})")
    print(f"  date:       {with_date} ({pct(with_date)})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        type=Path,
        help="Optional Exportify CSV to inspect for resolver identity coverage.",
    )
    args = parser.parse_args()
    rc = run_fixtures()
    if args.csv:
        inspect_csv(args.csv)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
