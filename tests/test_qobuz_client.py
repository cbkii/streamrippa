import hashlib
import logging
import os

import pytest
from util import arun

from streamrip.client.downloadable import BasicDownloadable
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.exceptions import (
    AuthenticationError,
    InvalidAppSecretError,
    MissingCredentialsError,
    NonStreamableError,
)

logger = logging.getLogger("streamrip")


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


def test_client_raises_missing_credentials():
    c = Config.defaults()
    client = QobuzClient(c)
    with pytest.raises(MissingCredentialsError):
        arun(client.login())
    arun(client.session.close())


def test_get_downloadable_requires_login():
    c = Config.defaults()
    client = QobuzClient(c)
    with pytest.raises(AuthenticationError, match="not logged in"):
        arun(client.get_downloadable("19512574", 3))


def test_get_downloadable_requires_secret():
    c = Config.defaults()
    client = QobuzClient(c)
    client.logged_in = True
    with pytest.raises(InvalidAppSecretError, match="Missing validated"):
        arun(client.get_downloadable("19512574", 3))


def test_get_downloadable_invalid_quality():
    c = Config.defaults()
    client = QobuzClient(c)
    client.logged_in = True
    client.secret = "secret"
    with pytest.raises(NonStreamableError, match="Unsupported Qobuz quality"):
        arun(client.get_downloadable("19512574", 0))


def test_get_downloadable_signing_failure_status_400(monkeypatch):
    c = Config.defaults()
    client = QobuzClient(c)
    client.logged_in = True
    client.secret = "secret"

    async def fake_request_file_url(*_args, **_kwargs):
        return 400, {"message": "Invalid request_sig"}

    monkeypatch.setattr(client, "_request_file_url", fake_request_file_url)
    with pytest.raises(NonStreamableError, match="signing failure"):
        arun(client.get_downloadable("19512574", 3))


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
