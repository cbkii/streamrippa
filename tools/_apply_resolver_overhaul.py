#!/usr/bin/env python3
"""One-shot patch runner for the CSV resolver overhaul branch.

This file is removed by the workflow after it applies and validates the source
changes.  Keeping the transformation deterministic makes the remote branch
mutation auditable and fail-closed against an unexpected base revision.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def replace_between(text: str, start: str, end: str, replacement: str, *, label: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"{label}: start marker not found")
    end_index = text.find(end, start_index)
    if end_index < 0:
        raise RuntimeError(f"{label}: end marker not found")
    return text[:start_index] + replacement + text[end_index:]


def patch_file_lists() -> None:
    path = ROOT / "streamrip/file_lists.py"
    text = path.read_text()

    text = replace_once(
        text,
        "import re\nimport warnings\n",
        "import re\nimport unicodedata\nimport warnings\n",
        label="unicodedata import",
    )
    text = replace_once(
        text,
        'duration_raw = (row.get("Duration (ms)") or "").strip()',
        'duration_raw = (\n                    row.get("Duration (ms)")\n                    or row.get("Track Duration (ms)")\n                    or ""\n                ).strip()',
        label="duration alias",
    )
    text = replace_once(
        text,
        'release_date=(row.get("Release Date") or "").strip(),',
        'release_date=(\n                        row.get("Release Date")\n                        or row.get("Album Release Date")\n                        or ""\n                    ).strip(),',
        label="release date alias",
    )
    text = replace_once(
        text,
        'genres=(row.get("Genres") or "").strip(),',
        'genres=(row.get("Genres") or row.get("Artist Genres") or "").strip(),',
        label="genres alias",
    )

    new_normalise = '''def _normalise(s: str) -> str:
    """Conservatively normalise catalogue identity text.

    Formatting differences are folded while meaningful words remain available
    to the title/variant model.  Latin diacritics are folded so provider
    transliteration differences do not prevent an otherwise strong match.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().strip()
    s = s.translate(
        str.maketrans(
            {
                "’": "'",
                "‘": "'",
                "`": "'",
                "“": '"',
                "”": '"',
                "–": "-",
                "—": "-",
                "−": "-",
            }
        )
    )
    s = s.replace("&", " and ")
    s = re.sub(r"[\\s\\-_]+", " ", s)
    s = re.sub(r"[^\\w\\s]", "", s)
    return re.sub(r"\\s+", " ", s).strip()
'''
    text = replace_between(
        text,
        "def _normalise(s: str) -> str:\n",
        "\n\ndef strip_title_decorators",
        new_normalise + "\n\n",
        label="normalise function",
    )

    neutral_helper = '''def _neutral_title_extension_match(
    row_title: ParsedTitle,
    candidate_title: ParsedTitle,
) -> bool:
    """Return whether titles differ only by a plausible neutral subtitle.

    The shorter title must appear as a contiguous token sequence in the longer
    title, the extension must be bounded, and known recording/version markers
    must agree.  Supporting artist/album/duration evidence is deliberately
    checked by the scorer rather than here.
    """
    if not row_title.normalized or not candidate_title.normalized:
        return False
    if row_title.normalized == candidate_title.normalized:
        return False
    if row_title.variants != candidate_title.variants:
        return False

    row_tokens = row_title.normalized.split()
    candidate_tokens = candidate_title.normalized.split()
    if len(row_tokens) <= len(candidate_tokens):
        short, long = row_tokens, candidate_tokens
    else:
        short, long = candidate_tokens, row_tokens

    extra = len(long) - len(short)
    if not short or extra < 1 or extra > 5:
        return False
    if len(short) / len(long) < 0.45:
        return False

    width = len(short)
    return any(long[i : i + width] == short for i in range(len(long) - width + 1))
'''
    marker = "\n\n@functools.lru_cache(maxsize=64)\ndef _resolve_bad_context_fields"
    if "def _neutral_title_extension_match(" in text:
        raise RuntimeError("neutral title helper already present")
    text = text.replace(marker, "\n\n" + neutral_helper + marker, 1)

    new_scorer = '''def _score_candidate_internal(
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
    isrc_match = bool(
        row.isrc and candidate_isrc and row.isrc.upper() == candidate_isrc.upper()
    )
    signals: dict[str, object] = {
        "isrc_match": isrc_match,
        "isrc_safety_veto": "",
        "row_variants": sorted(row_title.variants),
        "candidate_variants": sorted(cand_title.variants),
        "title_exact": False,
        "title_core": False,
        "title_neutral_extension": False,
        "title_token_containment": False,
        "title_fuzzy_guarded": False,
    }

    artist_inputs = row.artists_list or ([row.artists_raw] if row.artists_raw else [])
    artist_ok = _artist_overlap(artist_inputs, candidate_artist)
    row_album_norm = _normalise_variant_text(row.album)
    cand_album_norm = _normalise_variant_text(candidate_album)
    album_ok = bool(
        row.album
        and candidate_album
        and row_album_norm
        and cand_album_norm
        and (
            row_album_norm == cand_album_norm
            or row_album_norm in cand_album_norm
            or cand_album_norm in row_album_norm
        )
    )
    duration_delta = (
        abs(row.duration_ms - candidate_duration_ms)
        if row.duration_ms and candidate_duration_ms
        else None
    )
    duration_ok = bool(duration_delta is not None and duration_delta <= 12000)

    if isrc_match:
        if not artist_ok:
            signals["isrc_safety_veto"] = "artist-conflict"
            return CandidateExplanation(
                score=0,
                reason_codes=("reject_isrc_artist_conflict",),
                signals=signals,
            )
        variant_penalty, reject_variant = _variant_policy_penalty(
            row_title.variants,
            cand_title.variants,
            policy,
        )
        signals["variant_penalty"] = variant_penalty
        if reject_variant and row_title.variants != cand_title.variants:
            signals["isrc_safety_veto"] = "variant-conflict"
            return CandidateExplanation(
                score=0,
                reason_codes=("reject_isrc_variant_conflict",),
                signals=signals,
            )
        if duration_delta is not None:
            severe_delta = max(30000, int((row.duration_ms or 0) * 0.10))
            signals["duration_delta_ms"] = duration_delta
            if duration_delta >= severe_delta:
                signals["isrc_safety_veto"] = "duration-conflict"
                return CandidateExplanation(
                    score=0,
                    reason_codes=("reject_isrc_duration_conflict",),
                    signals=signals,
                )
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
            signals["isrc_safety_veto"] = "bad-context"
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
    neutral_extension = _neutral_title_extension_match(row_title, cand_title)
    neutral_extension = bool(neutral_extension and artist_ok and (album_ok or duration_ok))

    signals["title_exact"] = exact_title
    signals["title_core"] = core_title_match
    signals["title_neutral_extension"] = neutral_extension
    signals["title_token_containment"] = neutral_extension

    guarded_fuzzy_match = False
    if not exact_title and not core_title_match and not neutral_extension:
        if allow_guarded_fuzzy and policy.enable_guarded_fuzzy_normal:
            fuzzy_ratio = SequenceMatcher(
                None, row_title.normalized, cand_title.normalized
            ).ratio()
            signals["title_similarity"] = round(fuzzy_ratio, 4)
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

    score = 27
    if exact_title:
        score += 10
    if core_title_match:
        score += 8
    if neutral_extension:
        score += 7
        reasons.append("accepted_neutral_title_extension")
    if guarded_fuzzy_match:
        score += 6
        reasons.append("accepted_guarded_fuzzy")

    coverage = _artist_coverage(row.artists_list, candidate_artist)
    signals["artist_coverage"] = round(coverage, 3)
    if coverage >= 1.0:
        score += 26
    elif coverage >= 0.5:
        score += 14
    elif artist_ok:
        score += 8
    else:
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

    if album_ok:
        if row_album_norm == cand_album_norm:
            score += 4
        else:
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

    if row_title.variants and row_title.variants.issubset(cand_title.variants):
        score += 8
    score -= variant_penalty

    if duration_delta is not None:
        signals["duration_delta_ms"] = duration_delta
        if duration_delta <= 2500:
            score += 10
        elif duration_delta <= 6000:
            score += 6
        elif duration_delta <= 12000:
            score += 2
        elif duration_delta >= 30000:
            return CandidateExplanation(
                score=0, reason_codes=("reject_duration_far",), signals=signals
            )
        elif duration_delta >= 20000:
            score -= 16
        elif duration_delta >= 15000:
            score -= 10
        else:
            score -= 4

    return CandidateExplanation(
        score=max(score, 1), reason_codes=tuple(reasons), signals=signals
    )
'''
    text = replace_between(
        text,
        "def _score_candidate_internal(\n",
        "\n\ndef score_candidate(\n",
        new_scorer + "\n\n",
        label="candidate scorer",
    )

    path.write_text(text)


def patch_csv_playlist() -> None:
    path = ROOT / "streamrip/media/csv_playlist.py"
    text = path.read_text()

    text = replace_once(
        text,
        'REASON_QUALITY_UNAVAILABLE = "quality unavailable"\n',
        'REASON_QUALITY_UNAVAILABLE = "quality unavailable"\n'
        'REASON_NO_RESULTS_AFTER_BROAD = "no-results-after-broad-search"\n'
        'REASON_PROVIDER_SEARCH_ERROR = "provider-search-error"\n'
        'REASON_AMBIGUOUS = "ambiguous-candidates"\n'
        'REASON_TITLE_REJECTED = "candidates-found-but-title-rejected"\n'
        'REASON_ARTIST_REJECTED = "candidates-found-but-artist-rejected"\n'
        'REASON_VARIANT_CONFLICT = "variant-conflict"\n'
        'REASON_DURATION_CONFLICT = "duration-conflict"\n',
        label="resolver reason constants",
    )

    old_outcome = '''@dataclass(slots=True)
class ResolverOutcome:
    candidate: TrackCandidate | None
    reason: str
    query: str
    strategy: str
    rejected: list[TrackCandidate] | None = None
'''
    new_outcome = '''@dataclass(slots=True)
class ResolverOutcome:
    candidate: TrackCandidate | None
    reason: str
    query: str
    strategy: str
    rejected: list[TrackCandidate] | None = None
    attempts: tuple[dict[str, object], ...] = ()
'''
    text = replace_once(text, old_outcome, new_outcome, label="ResolverOutcome attempts")

    new_queries = '''_BROAD_SEARCH_STRATEGIES: frozenset[str] = frozenset(
    {"title-only", "canonical-title-only"}
)


def _build_search_queries(
    row: ExportifyCsvRow, source: str, *, escalation: bool = False
) -> list[tuple[str, str]]:
    """Build a deterministic adaptive query plan from strong to broad identity.

    Album and release year remain scoring evidence rather than mandatory terms
    in every provider query.  Broad title-only discovery is available in normal
    mode, but candidates still pass the same strict acceptance scorer.
    """
    first_artist = _first_artist(row)
    canonical_title = row.canonical_track_name or strip_title_decorators(row.track_name)

    queries: list[tuple[str, str]] = []
    if row.isrc and source in {"deezer", "qobuz"}:
        queries.append(("isrc", row.isrc))

    if row.album:
        queries.append(
            (
                "title-artist-album",
                " ".join(p for p in (row.track_name, first_artist, row.album) if p),
            )
        )
        if canonical_title and canonical_title != row.track_name:
            queries.append(
                (
                    "canonical-title-artist-album",
                    " ".join(
                        p for p in (canonical_title, first_artist, row.album) if p
                    ),
                )
            )

    queries.append(
        ("title-artist", " ".join(p for p in (row.track_name, first_artist) if p))
    )
    if canonical_title and canonical_title != row.track_name:
        queries.append(
            (
                "canonical-title-artist",
                " ".join(p for p in (canonical_title, first_artist) if p),
            )
        )

    queries.append(
        ("artist-title", " ".join(p for p in (first_artist, row.track_name) if p))
    )
    if canonical_title and canonical_title != row.track_name:
        queries.append(
            (
                "artist-canonical-title",
                " ".join(p for p in (first_artist, canonical_title) if p),
            )
        )

    if row.album:
        queries.append(
            ("title-album", " ".join(p for p in (row.track_name, row.album) if p))
        )
        if canonical_title and canonical_title != row.track_name:
            queries.append(
                (
                    "canonical-title-album",
                    " ".join(p for p in (canonical_title, row.album) if p),
                )
            )

    # Broad discovery is normal-mode capable.  Acceptance remains strict.
    queries.append(("title-only", row.track_name))
    if canonical_title and canonical_title != row.track_name:
        queries.append(("canonical-title-only", canonical_title))

    if escalation and row.album:
        queries.append(
            (
                "album-title-artist",
                " ".join(p for p in (row.album, row.track_name, first_artist) if p),
            )
        )

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for strategy, query in queries:
        qn = " ".join(query.split())
        key = qn.casefold()
        if qn and key not in seen:
            seen.add(key)
            out.append((strategy, qn))
    return out


def _select_best_candidate(
    hits: list[tuple[str, str, TrackCandidate]],
) -> tuple[str, str, TrackCandidate] | None:
    """Select the strongest discovered candidate while preserving tie order."""
    if not hits:
        return None
    confidence_rank = {CONF_REJECT: 0, CONF_LOW: 1, CONF_MEDIUM: 2, CONF_HIGH: 3}
    _, best = max(
        enumerate(hits),
        key=lambda item: (
            item[1][2].score,
            confidence_rank.get(item[1][2].confidence, 0),
            -item[0],
        ),
    )
    return best


def _rejection_reason(candidate: TrackCandidate | None) -> str:
    if candidate is None:
        return REASON_NO_RESULTS
    reasons = set(candidate.reason_codes)
    if "reject_title_mismatch" in reasons:
        return REASON_TITLE_REJECTED
    if "reject_artist_mismatch" in reasons or "reject_isrc_artist_conflict" in reasons:
        return REASON_ARTIST_REJECTED
    if "reject_variant_policy" in reasons or "reject_isrc_variant_conflict" in reasons:
        return REASON_VARIANT_CONFLICT
    if "reject_duration_far" in reasons or "reject_isrc_duration_conflict" in reasons:
        return REASON_DURATION_CONFLICT
    if "reject_bad_context" in reasons:
        return "bad-context-conflict"
    return "candidates-found-but-rejected"
'''
    text = replace_between(
        text,
        "def _build_search_queries(\n",
        "\n\ndef _pick_best_candidate(\n",
        new_queries + "\n\n",
        label="search query plan",
    )

    new_resolver = '''        async def _resolve_for_client(
            client: Client,
            *,
            escalation: bool = False,
        ) -> ResolverOutcome:
            """Discover broadly, then choose the strongest strictly accepted match."""
            queries = _build_search_queries(row, client.source, escalation=escalation)
            provider_min_score = _provider_threshold(
                csv_cfg, client.source, repair_mode=self.repair_mode
            )
            if (
                self.provider_budgets is not None
                and self.provider_budgets[client.source].disabled
            ):
                return ResolverOutcome(
                    candidate=None,
                    reason="provider disabled (auth/session failure)",
                    query="",
                    strategy="provider-disabled",
                )

            best_low_conf: tuple[str, str, TrackCandidate] | None = None
            closest_rejected: list[TrackCandidate] = []
            best_score_by_id: dict[str, int] = {}
            matched_hits: list[tuple[str, str, TrackCandidate]] = []
            attempts: list[dict[str, object]] = []
            had_error = False
            last_query = ""
            last_strategy = ""

            def _add_rejected(candidate: TrackCandidate, reason_code: str = "") -> None:
                reason_codes = candidate.reason_codes
                if reason_code and reason_code not in reason_codes:
                    reason_codes = tuple((*reason_codes, reason_code))
                enriched = TrackCandidate(
                    source=candidate.source,
                    id=candidate.id,
                    title=candidate.title,
                    artist=candidate.artist,
                    album=candidate.album,
                    release_date=candidate.release_date,
                    isrc=candidate.isrc,
                    score=candidate.score,
                    client=candidate.client,
                    reason_codes=reason_codes,
                    signals=candidate.signals,
                    confidence=candidate.confidence,
                    margin_to_second=candidate.margin_to_second,
                )
                for index, existing in enumerate(closest_rejected):
                    if (existing.source, existing.id) == (enriched.source, enriched.id):
                        if enriched.score > existing.score:
                            closest_rejected[index] = enriched
                        return
                closest_rejected.append(enriched)
                closest_rejected.sort(key=lambda c: c.score, reverse=True)
                del closest_rejected[4:]

            def _record_hit(
                query: str, strategy: str, candidate: TrackCandidate
            ) -> None:
                for index, (_, _, existing) in enumerate(matched_hits):
                    if existing.id == candidate.id and existing.source == candidate.source:
                        if candidate.score > existing.score:
                            matched_hits[index] = (query, strategy, candidate)
                        return
                matched_hits.append((query, strategy, candidate))

            def _refresh_candidate_confidence(candidate: TrackCandidate) -> None:
                candidate.margin_to_second = _margin_to_second_best(
                    candidate.score, list(best_score_by_id.values())
                )
                candidate.confidence = _confidence_for_candidate(
                    candidate, provider_min_score, candidate.margin_to_second
                )

            hinted_id = ""
            if self.repair_mode and row.repair_candidate_ids:
                hinted_id = (row.repair_candidate_ids.get(client.source) or "").strip()

            if hinted_id and not escalation:
                try:
                    if (
                        self.provider_budgets is not None
                        and client.source in self.provider_budgets
                    ):
                        async with self.provider_budgets[client.source].metadata_sem:
                            await self._provider_wait(client.source)
                            hinted_resp = await client.get_metadata(hinted_id, "track")
                    else:
                        hinted_resp = await client.get_metadata(hinted_id, "track")
                    self._provider_after_call(client.source, ok=True, err=None)
                    explain = explain_candidate_score(
                        row,
                        _item_title(client.source, hinted_resp),
                        _item_artist(client.source, hinted_resp),
                        _item_album(client.source, hinted_resp),
                        _item_date(client.source, hinted_resp),
                        _item_isrc(client.source, hinted_resp),
                        _item_duration_ms(client.source, hinted_resp),
                        policy=match_policy,
                    )
                    hinted_score = (
                        score_candidate_repair(
                            row,
                            _item_title(client.source, hinted_resp),
                            _item_artist(client.source, hinted_resp),
                            _item_album(client.source, hinted_resp),
                            _item_date(client.source, hinted_resp),
                            _item_isrc(client.source, hinted_resp),
                            _item_duration_ms(client.source, hinted_resp),
                            policy=match_policy,
                        )
                        if self.repair_mode
                        else explain.score
                    )
                    hinted_candidate = TrackCandidate(
                        source=client.source,
                        id=str(hinted_resp.get("id", hinted_id)),
                        title=_item_title(client.source, hinted_resp),
                        artist=_item_artist(client.source, hinted_resp),
                        album=_item_album(client.source, hinted_resp),
                        release_date=_item_date(client.source, hinted_resp),
                        isrc=_item_isrc(client.source, hinted_resp),
                        score=hinted_score,
                        client=client,
                        reason_codes=explain.reason_codes,
                        signals=explain.signals,
                    )
                    best_score_by_id[hinted_candidate.id] = hinted_candidate.score
                    _refresh_candidate_confidence(hinted_candidate)
                    attempts.append(
                        {
                            "strategy": "id-hint",
                            "query": hinted_id,
                            "result_count": 1,
                            "shortlist_count": 1,
                            "error": "",
                        }
                    )
                    if hinted_candidate.score >= provider_min_score:
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
                    elif hinted_candidate.score > 0:
                        best_low_conf = (hinted_id, "id-hint", hinted_candidate)
                        _add_rejected(hinted_candidate, "reject_below_threshold")
                    else:
                        _add_rejected(hinted_candidate)
                except Exception as e:
                    self._provider_after_call(client.source, ok=False, err=e)
                    attempts.append(
                        {
                            "strategy": "id-hint",
                            "query": hinted_id,
                            "result_count": 0,
                            "shortlist_count": 0,
                            "error": type(e).__name__,
                        }
                    )
                    logger.debug(
                        "Hinted metadata lookup failed on %s id=%s: %s",
                        client.source,
                        hinted_id,
                        e,
                    )

            for strategy, query in queries:
                if (
                    self.provider_budgets is not None
                    and self.provider_budgets[client.source].disabled
                ):
                    break

                # Do not spend a broad title-only call when a previous strong or
                # alternate query already produced a clearly separated match.
                if strategy in _BROAD_SEARCH_STRATEGIES and matched_hits:
                    provisional = _select_best_candidate(matched_hits)
                    if provisional is not None:
                        _refresh_candidate_confidence(provisional[2])
                        if (
                            provisional[2].score >= provider_min_score + 10
                            and provisional[2].confidence in {CONF_MEDIUM, CONF_HIGH}
                        ):
                            break

                last_query = query
                last_strategy = strategy
                top_candidates: list[TrackCandidate] = []
                pages: list[dict] = []
                try:
                    effective_limit = (
                        escalation_limit
                        if escalation or strategy in _BROAD_SEARCH_STRATEGIES
                        else search_limit
                    )
                    cache_key = (client.source, query.casefold(), effective_limit)
                    if self.query_cache is not None and cache_key in self.query_cache:
                        pages = self.query_cache[cache_key]
                    else:
                        if self.provider_budgets is not None:
                            async with self.provider_budgets[client.source].search_sem:
                                await self._provider_wait(client.source)
                                pages = await client.search(
                                    "track", query, limit=effective_limit
                                )
                        else:
                            pages = await client.search(
                                "track", query, limit=effective_limit
                            )
                        self._provider_after_call(client.source, ok=True, err=None)
                        if self.query_cache is not None:
                            self.query_cache[cache_key] = pages

                    raw_items = _extract_raw_results(client.source, pages)
                    top_candidates = _pick_top_candidates(
                        row,
                        client.source,
                        pages,
                        client,
                        repair_mode=self.repair_mode,
                        limit=_SHORTLIST_K,
                        policy=match_policy,
                    )
                    attempts.append(
                        {
                            "strategy": strategy,
                            "query": query,
                            "result_count": len(raw_items),
                            "shortlist_count": len(top_candidates),
                            "error": "",
                        }
                    )
                    for rejected in _top_rejected_candidates(
                        row,
                        client.source,
                        pages,
                        client,
                        policy=match_policy,
                        limit=2,
                    ):
                        _add_rejected(rejected)

                    for cand in top_candidates:
                        best_score_by_id[cand.id] = max(
                            best_score_by_id.get(cand.id, 0), cand.score
                        )
                    for cand in top_candidates:
                        _refresh_candidate_confidence(cand)
                except Exception as e:
                    self._provider_after_call(client.source, ok=False, err=e)
                    attempts.append(
                        {
                            "strategy": strategy,
                            "query": query,
                            "result_count": 0,
                            "shortlist_count": 0,
                            "error": type(e).__name__,
                        }
                    )
                    if (
                        self.provider_budgets is not None
                        and self.provider_budgets[client.source].disabled
                    ):
                        break
                    had_error = True
                    logger.debug(
                        "Search failed on %s (%s) for '%s': %s",
                        client.source,
                        strategy,
                        query,
                        e,
                    )
                    continue

                for candidate in top_candidates:
                    reason = _candidate_reason(candidate, provider_min_score)
                    if reason == "matched":
                        _record_hit(query, strategy, candidate)
                        if (
                            candidate.score == 100
                            and "accepted_isrc_match" in candidate.reason_codes
                        ):
                            return ResolverOutcome(
                                candidate=candidate,
                                reason="matched",
                                query=query,
                                strategy=strategy,
                                rejected=closest_rejected[:2],
                                attempts=tuple(attempts),
                            )
                    else:
                        if (
                            best_low_conf is None
                            or candidate.score > best_low_conf[2].score
                        ):
                            best_low_conf = (query, strategy, candidate)
                        _add_rejected(candidate, "reject_below_threshold")

            selected_hit = _select_best_candidate(matched_hits)
            if selected_hit is not None:
                query, strategy, candidate = selected_hit
                _refresh_candidate_confidence(candidate)
                if (
                    strategy in _BROAD_SEARCH_STRATEGIES
                    and candidate.margin_to_second <= 2
                    and candidate.score < 95
                ):
                    return ResolverOutcome(
                        candidate=candidate,
                        reason=REASON_AMBIGUOUS,
                        query=query,
                        strategy=strategy,
                        rejected=closest_rejected[:2],
                        attempts=tuple(attempts),
                    )
                return ResolverOutcome(
                    candidate=candidate,
                    reason="matched",
                    query=query,
                    strategy=strategy,
                    rejected=closest_rejected[:2],
                    attempts=tuple(attempts),
                )

            if best_low_conf is not None:
                query, strategy, candidate = best_low_conf
                return ResolverOutcome(
                    candidate=candidate,
                    reason=_candidate_reason(candidate, provider_min_score),
                    query=query,
                    strategy=strategy,
                    rejected=closest_rejected[:2],
                    attempts=tuple(attempts),
                )

            if closest_rejected:
                candidate = closest_rejected[0]
                return ResolverOutcome(
                    candidate=candidate,
                    reason=_rejection_reason(candidate),
                    query=last_query,
                    strategy=last_strategy,
                    rejected=closest_rejected[:2],
                    attempts=tuple(attempts),
                )

            if had_error:
                return ResolverOutcome(
                    candidate=None,
                    reason=REASON_PROVIDER_SEARCH_ERROR,
                    query=last_query,
                    strategy=last_strategy,
                    attempts=tuple(attempts),
                )

            attempted_broad = any(
                str(attempt.get("strategy", "")) in _BROAD_SEARCH_STRATEGIES
                for attempt in attempts
            )
            return ResolverOutcome(
                candidate=None,
                reason=(REASON_NO_RESULTS_AFTER_BROAD if attempted_broad else REASON_NO_RESULTS),
                query=" || ".join(str(a.get("query", "")) for a in attempts if a.get("query")),
                strategy=" / ".join(
                    str(a.get("strategy", "")) for a in attempts if a.get("strategy")
                ),
                rejected=closest_rejected[:2],
                attempts=tuple(attempts),
            )

'''
    text = replace_between(
        text,
        "        async def _resolve_for_client(\n",
        "        primary_outcome = await _resolve_for_client(\n",
        new_resolver,
        label="client resolver",
    )

    # Acceptance must include the outcome classification, not only the numeric
    # score; otherwise an ambiguity veto could still leak through.
    text = text.replace(
        "primary_outcome.candidate is not None\n            and primary_outcome.candidate.score >= primary_min_score",
        'primary_outcome.reason == "matched"\n            and primary_outcome.candidate is not None\n            and primary_outcome.candidate.score >= primary_min_score',
    )
    text = text.replace(
        "primary_outcome.candidate is None\n            or primary_outcome.candidate.score < primary_min_score",
        'primary_outcome.reason != "matched"\n            or primary_outcome.candidate is None\n            or primary_outcome.candidate.score < primary_min_score',
    )
    text = text.replace(
        "fallback_outcome.candidate is None\n                or fallback_outcome.candidate.score < fallback_min_score",
        'fallback_outcome.reason != "matched"\n                or fallback_outcome.candidate is None\n                or fallback_outcome.candidate.score < fallback_min_score',
    )

    old_primary_candidate = '''        primary_candidate = (
            primary_outcome.candidate
            if primary_outcome.candidate
            and primary_outcome.candidate.score >= primary_min_score
            else None
        )
        fallback_candidate = (
            fallback_outcome.candidate
            if fallback_outcome.candidate
            and fallback_outcome.candidate.score >= fallback_min_score
            else None
        )
'''
    new_primary_candidate = '''        primary_candidate = (
            primary_outcome.candidate
            if primary_outcome.reason == "matched"
            and primary_outcome.candidate
            and primary_outcome.candidate.score >= primary_min_score
            else None
        )
        fallback_candidate = (
            fallback_outcome.candidate
            if fallback_outcome.reason == "matched"
            and fallback_outcome.candidate
            and fallback_outcome.candidate.score >= fallback_min_score
            else None
        )
'''
    text = replace_once(
        text,
        old_primary_candidate,
        new_primary_candidate,
        label="outcome-gated candidate selection",
    )

    text = replace_once(
        text,
        '                        "rejected": _rejected_candidate_payloads(\n                            primary_outcome.rejected\n                        ),\n',
        '                        "rejected": _rejected_candidate_payloads(\n                            primary_outcome.rejected\n                        ),\n'
        '                        "attempts": list(primary_outcome.attempts),\n',
        label="primary attempts telemetry",
    )
    text = replace_once(
        text,
        '                        "rejected": _rejected_candidate_payloads(\n                            fallback_outcome.rejected\n                        ),\n',
        '                        "rejected": _rejected_candidate_payloads(\n                            fallback_outcome.rejected\n                        ),\n'
        '                        "attempts": list(fallback_outcome.attempts),\n',
        label="fallback attempts telemetry",
    )

    path.write_text(text)


def patch_readme() -> None:
    path = ROOT / "README.md"
    text = path.read_text()
    marker = "## CSV resolver matching reliability"
    if marker in text:
        raise RuntimeError("README resolver section already present")
    text += '''\n\n## CSV resolver matching reliability\n\nExportify CSV resolution uses **broad candidate discovery with strict candidate\nacceptance**. Queries start with ISRC/title/artist/album identity, then relax\nprogressively to alternate term order and bounded title-only discovery when a\nprovider search is too literal. Candidates from multiple query strategies are\nranked together instead of accepting the first marginal match.\n\nProvider naming differences such as `Hard Twelve` versus `Hard Twelve (The\nAnte)` can be accepted as a neutral title extension only when artist identity\nand album or duration evidence also agree. Material alternatives such as\n`Hard Twelve (Live)`, acoustic/instrumental/remix versions, karaoke/tribute\nentries, and large duration conflicts remain guarded by variant/context rules.\n\nThe Exportify parser also accepts common equivalent columns including `Album\nRelease Date`, `Track Duration (ms)`, `Artist Genres`, and `Track ISRC`.\nDetailed per-query attempts and rejection signals are available through the CSV\nresolver telemetry/unresolved diagnostics.\n'''
    path.write_text(text)


def main() -> None:
    patch_file_lists()
    patch_csv_playlist()
    patch_readme()
    print("resolver overhaul patch applied")


if __name__ == "__main__":
    main()
