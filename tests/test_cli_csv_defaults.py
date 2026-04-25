from __future__ import annotations

from types import SimpleNamespace

from streamrip.rip.cli import _effective_csv_sources


def _cfg(csv_source: str, csv_fallback: str, lastfm_source: str, lastfm_fallback: str):
    return SimpleNamespace(
        session=SimpleNamespace(
            csv_resolver=SimpleNamespace(
                default_source=csv_source,
                default_fallback_source=csv_fallback,
            ),
            lastfm=SimpleNamespace(
                source=lastfm_source,
                fallback_source=lastfm_fallback,
            ),
        )
    )


def test_effective_csv_sources_cli_overrides_config():
    cfg = _cfg("qobuz", "deezer", "qobuz", "deezer")
    source, fallback = _effective_csv_sources(cfg, "tidal", "soundcloud")
    assert source == "tidal"
    assert fallback == "soundcloud"


def test_effective_csv_sources_prefers_csv_defaults_when_cli_omitted():
    cfg = _cfg("deezer", "qobuz", "qobuz", "deezer")
    source, fallback = _effective_csv_sources(cfg, None, None)
    assert source == "deezer"
    assert fallback == "qobuz"


def test_effective_csv_sources_falls_back_to_lastfm_then_safe_defaults():
    cfg = _cfg("", "", "qobuz", "")
    source, fallback = _effective_csv_sources(cfg, None, None)
    assert source == "qobuz"
    assert fallback == "deezer"

    cfg_empty = _cfg("", "", "", "")
    source, fallback = _effective_csv_sources(cfg_empty, None, None)
    assert source == "qobuz"
    assert fallback == "deezer"


def test_effective_csv_sources_cli_equal_source_and_fallback_rewrites_fallback():
    # When CLI passes the same value for both source and fallback,
    # the guard should rewrite the fallback to avoid a no-op.
    cfg = _cfg("", "", "", "")
    source, fallback = _effective_csv_sources(cfg, "deezer", "deezer")
    assert source == "deezer"
    assert fallback == "qobuz"

    # Same guard for the non-deezer case.
    source2, fallback2 = _effective_csv_sources(cfg, "qobuz", "qobuz")
    assert source2 == "qobuz"
    assert fallback2 == "deezer"
