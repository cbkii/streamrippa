import hashlib
import logging
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from util import arun

from streamrip.client.downloadable import BasicDownloadable
from streamrip.client.qobuz import QobuzClient, QobuzSpoofer
from streamrip.config import Config
from streamrip.exceptions import (
    AuthenticationError,
    InvalidAppSecretError,
    MissingCredentialsError,
    NonStreamableError,
)

logger = logging.getLogger("streamrip")


@pytest.fixture
def client():
    qobuz_client = QobuzClient(Config.defaults())
    yield qobuz_client
    if hasattr(qobuz_client, "session") and qobuz_client.session is not None:
        arun(qobuz_client.session.close())


@pytest.fixture(scope="session")
def qobuz_client():
    config = Config.defaults()
    config.session.qobuz.email_or_userid = os.environ["QOBUZ_EMAIL"]
    config.session.qobuz.password_or_token = hashlib.md5(
        os.environ["QOBUZ_PASSWORD"].encode("utf-8"),
    ).hexdigest()
    if "QOBUZ_APP_ID" in os.environ and "QOBUZ_SECRETS" in os.environ:
        config.session.qobuz.app_id = os.environ["QOBUZ_APP_ID"]
        config.session.qobuz.secrets = os.environ["QOBUZ_SECRETS"].split(",")
    client = QobuzClient(config)
    arun(client.login())

    yield client

    arun(client.session.close())


def test_client_raises_missing_credentials(client):
    with pytest.raises(MissingCredentialsError):
        arun(client.login())


def test_get_downloadable_requires_login(client):
    with pytest.raises(AuthenticationError, match="not logged in"):
        arun(client.get_downloadable("19512574", 3))


def test_get_downloadable_requires_secret(client):
    client.logged_in = True
    with pytest.raises(InvalidAppSecretError, match="Missing validated"):
        arun(client.get_downloadable("19512574", 3))


def test_get_downloadable_invalid_quality(client):
    client.logged_in = True
    client.secret = "secret"
    with pytest.raises(NonStreamableError, match="Unsupported Qobuz quality"):
        arun(client.get_downloadable("19512574", 0))


def test_get_downloadable_signing_failure_status_400(monkeypatch, client):
    client.logged_in = True
    client.secret = "secret"

    async def fake_request_file_url(*_args, **_kwargs):
        return 400, {"message": "Invalid request_sig"}

    monkeypatch.setattr(client, "_request_file_url", fake_request_file_url)
    with pytest.raises(NonStreamableError, match="signing failure"):
        arun(client.get_downloadable("19512574", 3))


def test_extract_bundle_urls_normalizes_and_filters_js_paths():
    login_page = """
    <html>
      <script src="/resources/abc.bundle.js?v=1"></script>
      <script src="//play.qobuz.com/resources/def.bundle.js?cache=2"></script>
      <script src="https://cdn.qobuz.com/resources/ghi.bundle.js?x=3#hash"></script>
      <script src="/resources/not-js.css?v=1"></script>
      <script src="/app.bundle.js?v=1"></script>
    </html>
    """

    assert QobuzSpoofer._extract_bundle_urls(login_page) == [
        "https://play.qobuz.com/resources/abc.bundle.js?v=1",
        "https://play.qobuz.com/resources/def.bundle.js?cache=2",
        "https://cdn.qobuz.com/resources/ghi.bundle.js?x=3",
    ]


@pytest.mark.asyncio
async def test_get_app_id_and_secrets_skips_empty_bundle_results():
    spoofer = QobuzSpoofer()
    spoofer.session = MagicMock()

    login_resp = AsyncMock()
    login_resp.text = AsyncMock(
        return_value="<script src='/resources/a.bundle.js'></script>"
    )
    login_ctx = AsyncMock()
    login_ctx.__aenter__.return_value = login_resp
    login_ctx.__aexit__.return_value = False

    first_bundle_resp = AsyncMock()
    first_bundle_resp.text = AsyncMock(return_value="first")
    first_ctx = AsyncMock()
    first_ctx.__aenter__.return_value = first_bundle_resp
    first_ctx.__aexit__.return_value = False

    second_bundle_resp = AsyncMock()
    second_bundle_resp.text = AsyncMock(return_value="second")
    second_ctx = AsyncMock()
    second_ctx.__aenter__.return_value = second_bundle_resp
    second_ctx.__aexit__.return_value = False

    spoofer.session.get.side_effect = [login_ctx, first_ctx, second_ctx]
    spoofer._extract_bundle_urls = lambda _page: [
        "https://play.qobuz.com/resources/one.bundle.js",
        "https://play.qobuz.com/resources/two.bundle.js",
    ]

    calls = {"n": 0}

    def _fake_extract(bundle):
        calls["n"] += 1
        if bundle == "first":
            return "123", []
        return "456", ["secret"]

    spoofer._extract_app_id_and_secrets_from_bundle = _fake_extract

    app_id, secrets = await spoofer.get_app_id_and_secrets()
    assert app_id == "456"
    assert secrets == ["secret"]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_paginate_batches_requests(monkeypatch, client):
    calls = []

    async def _fake_api_request(epoint, params):
        calls.append(params.get("offset", 0))
        offset = params.get("offset", 0)
        return (
            200,
            {
                "albums": {
                    "items": [{"id": offset}],
                    "total": 1300,
                    "limit": 100,
                    "offset": offset,
                }
            },
        )

    monkeypatch.setattr(client, "_api_request", _fake_api_request)
    pages = await client._paginate("album/search", {"query": "x"}, limit=1300)
    assert len(pages) == 13


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_client_get_metadata(qobuz_client):
    meta = arun(qobuz_client.get_metadata("s9nzkwg2rh1nc", "album"))
    assert meta["title"] == "I Killed Your Dog"
    assert len(meta["tracks"]["items"]) == 16
    assert meta["maximum_bit_depth"] == 24


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_client_get_downloadable(qobuz_client):
    d = arun(qobuz_client.get_downloadable("19512574", 3))
    assert isinstance(d, BasicDownloadable)
    assert d.extension == "flac"
    assert isinstance(d.url, str)
    assert "https://" in d.url


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_client_search_limit(qobuz_client):
    res = qobuz_client.search("album", "rumours", limit=5)
    total = 0
    for r in arun(res):
        total += len(r["albums"]["items"])
    assert total == 5


@pytest.mark.skipif(
    "QOBUZ_EMAIL" not in os.environ, reason="Qobuz credentials not found in env."
)
def test_client_search_no_limit(qobuz_client):
    # Setting no limit has become impossible because `limit: int` now
    res = qobuz_client.search("album", "rumours", limit=10000)
    correct_total = 0
    total = 0
    for r in arun(res):
        total += len(r["albums"]["items"])
        correct_total = max(correct_total, r["albums"]["total"])
    assert total == correct_total
