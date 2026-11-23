import asyncio
import traceback
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from streamrip.client.deezer import DeezerClient
from streamrip.client.qobuz import QobuzClient
from streamrip.config import Config
from streamrip.exceptions import (
    AuthenticationError,
    InvalidAppIdError,
    InvalidAppSecretError,
    QobuzRequestError,
)
from streamrip.rip.prompter import QobuzPrompter

TOKEN = "synthetic-credential-for-redaction"
APP_ID = "synthetic-app"
SECRET = "synthetic-signing-secret"
FAVORITES = {"albums": {"items": []}}


def reply(status, mime="application/json", body=None):
    response = MagicMock(status=status, content_type=mime)
    response.json = AsyncMock(return_value=body)
    if isinstance(body, Exception):
        response.json.side_effect = body
    context = MagicMock()
    context.__aenter__.return_value = response
    return context


def leaking_error():
    url = URL("https://example.invalid/user/login").with_query(user_auth_token=TOKEN)
    info = aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
    return aiohttp.ContentTypeError(info, (), status=403, message="unexpected mimetype")


@pytest.fixture
def account():
    cfg = Config.defaults()
    c = cfg.session.qobuz
    c.use_auth_token = True
    c.app_id, c.secrets, c.password_or_token = APP_ID, [SECRET], TOKEN
    cfg.session.downloads.api_retry_delay_seconds = 0
    cfg.session.downloads.api_request_retries = 2
    client = QobuzClient(cfg)
    session = MagicMock(headers={})
    session.close = AsyncMock()
    client.get_session = AsyncMock(return_value=session)
    return client, session


async def test_cached_token_concurrent_then_repeated_login(account):
    client, session = account
    session.request.return_value = reply(200, body=FAVORITES)
    assert QobuzPrompter(client.config, client).has_creds()
    await asyncio.gather(client.login(), client.login())
    await client.login()
    method, url = session.request.call_args.args
    options = session.request.call_args.kwargs
    assert method == "GET" and url.endswith("favorite/getUserFavorites")
    assert options["headers"] == {"X-App-Id": APP_ID, "X-User-Auth-Token": TOKEN}
    assert TOKEN not in url and TOKEN not in str(options["params"])
    assert options["allow_redirects"] is False
    assert session.request.call_count == 1 and session.headers == {}
    assert client.logged_in and not client.config.file.modified


async def test_password_post_and_token_scoped_to_api(account):
    client, session = account
    c = client.config.session.qobuz
    c.use_auth_token = False
    c.email_or_userid = "user@example.invalid"
    session.request.return_value = reply(
        200,
        body={
            "user": {"credential": {"parameters": {"streaming": True}}},
            "user_auth_token": TOKEN,
        },
    )
    await client.login()
    method, url = session.request.call_args.args
    options = session.request.call_args.kwargs
    assert method == "POST" and url.endswith("user/login")
    assert options["data"] == {
        "email": c.email_or_userid,
        "password": c.password_or_token,
    }
    assert "params" not in options and "X-User-Auth-Token" not in options["headers"]
    assert not session.headers


@pytest.mark.parametrize(
    "status,mime,body,error,match,attempts",
    [
        (
            400,
            "application/json",
            {"message": "Invalid app_id " + TOKEN},
            InvalidAppIdError,
            "ID rejected",
            1,
        ),
        (
            401,
            "application/json",
            {"message": "Invalid token " + TOKEN},
            AuthenticationError,
            "rejected",
            1,
        ),
        (
            401,
            "application/json",
            {"message": "Expired token " + TOKEN},
            AuthenticationError,
            "expired",
            1,
        ),
        (403, "text/html", None, QobuzRequestError, "403, text/html", 1),
        (401, "text/plain", None, QobuzRequestError, "401, text/plain", 1),
        (302, "text/html", None, QobuzRequestError, "302, text/html", 1),
        (408, "text/plain", None, QobuzRequestError, "408, text/plain", 2),
        (429, "text/html", None, QobuzRequestError, "429, text/html", 2),
        (503, "text/html", None, QobuzRequestError, "503, text/html", 2),
        (
            200,
            "application/json",
            ValueError(TOKEN),
            QobuzRequestError,
            "JSON object",
            1,
        ),
        (200, "application/json", None, QobuzRequestError, "JSON object", 1),
        (200, "application/json", {"albums": None}, QobuzRequestError, "favorites", 1),
    ],
)
async def test_errors_are_classified_without_replaying_login(
    account, caplog, status, mime, body, error, match, attempts
):
    client, session = account
    session.request.return_value = reply(status, mime, body)
    with pytest.raises(error, match=match) as caught:
        await client.login()
    assert session.request.call_count == attempts
    assert not client.logged_in and client._token == ""
    session.close.assert_awaited_once()
    assert TOKEN not in caplog.text + "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "error,attempts",
    [
        (leaking_error(), 1),
        (aiohttp.ClientConnectionError("connection", leaking_error()), 2),
        (asyncio.TimeoutError(TOKEN), 2),
    ],
)
async def test_nested_url_exception_never_reaches_retry_log_or_traceback(
    account, caplog, error, attempts
):
    client, session = account
    session.request.side_effect = error
    with pytest.raises(QobuzRequestError) as caught:
        await client.login()
    assert TOKEN not in caplog.text + "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert session.request.call_count == attempts
    session.request.side_effect = None
    session.request.return_value = reply(200, body=FAVORITES)
    await client.login()
    assert client.logged_in


async def test_reported_aiohttp_content_type_error_on_mock_server(monkeypatch):
    from streamrip.client import qobuz

    async def forbidden(request):
        return web.Response(status=403, content_type="text/html", text="access denied")

    app = web.Application()
    app.router.add_get("/{endpoint:.*}", forbidden)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                base + "/user/login", params={"user_auth_token": TOKEN}
            ) as response:
                with pytest.raises(aiohttp.ContentTypeError) as legacy:
                    await response.json()
                assert TOKEN in str(legacy.value)
        monkeypatch.setattr(qobuz, "QOBUZ_BASE_URL", base)
        cfg = Config.defaults()
        cfg.session.qobuz.use_auth_token = True
        cfg.session.qobuz.app_id = APP_ID
        cfg.session.qobuz.password_or_token = TOKEN
        client = QobuzClient(cfg)
        with pytest.raises(QobuzRequestError, match="403, text/html") as fixed:
            await client.login()
        assert TOKEN not in str(fixed.value) and client.session.closed
    finally:
        await runner.cleanup()


async def test_signed_url_only_after_auth_and_explicit_signature_rejection(account):
    client, session = account
    client.config.session.qobuz.secrets = ["synthetic-stale", SECRET]
    session.request.side_effect = [
        reply(503, "text/html"),
        reply(200, body=FAVORITES),
        reply(400, body={"message": "Invalid request_signature"}),
        reply(200, body={"url": "https://media.invalid/audio?sig=" + TOKEN}),
        reply(200, body={"url": "https://media.invalid/another"}),
    ]
    await client.login()
    assert client.secret is None
    first, _ = await asyncio.gather(
        client.get_downloadable("123", 2), client.get_downloadable("124", 2)
    )
    assert client.secret == SECRET and session.request.call_count == 5
    assert not session.headers and TOKEN not in repr(first)
    assert TOKEN not in repr(client.config.session)


@pytest.mark.parametrize(
    "status,body,error",
    [
        (400, {"message": "Invalid request_sig"}, InvalidAppSecretError),
        (401, {"message": "bad token"}, AuthenticationError),
        (410, {}, QobuzRequestError),
        (400, {"message": "unrelated invalid format"}, QobuzRequestError),
    ],
)
async def test_streaming_failure_is_separate_from_auth(account, status, body, error):
    client, session = account
    session.request.side_effect = [reply(200, body=FAVORITES), reply(status, body=body)]
    await client.login()
    with pytest.raises(error) as caught:
        await client.get_downloadable("123", 2)
    assert client.logged_in and client.secret is None
    assert SECRET not in str(caught.value)


async def test_failed_discovery_does_not_replace_cached_id(account):
    client, session = account
    client.config.session.qobuz.secrets = []
    client._get_app_id_and_secrets = AsyncMock(return_value=("different", [SECRET]))
    session.request.return_value = reply(200, body=FAVORITES)
    await client.login()
    with pytest.raises(InvalidAppIdError):
        await client.get_downloadable("123", 2)
    assert (
        not client.config.file.modified and client.config.session.qobuz.app_id == APP_ID
    )


async def test_deezer_still_uses_arl(monkeypatch):
    cfg = Config.defaults()
    cfg.session.deezer.arl = "synthetic-arl"
    client = DeezerClient(cfg)
    login = MagicMock(return_value=True)
    monkeypatch.setattr(client.client, "login_via_arl", login)
    client.get_session = AsyncMock()
    await client.login()
    login.assert_called_once_with("synthetic-arl")
    assert client.logged_in


async def test_discovered_pair_committed_only_after_validation(account):
    client, session = account
    client.config.session.qobuz.app_id = ""
    client.config.session.qobuz.secrets = []
    client._get_app_id_and_secrets = AsyncMock(return_value=(APP_ID, [SECRET]))
    session.request.side_effect = [reply(403, "text/html"), reply(200, body=FAVORITES)]
    with pytest.raises(QobuzRequestError):
        await asyncio.gather(client.login(), client.login())
    assert not client.config.file.modified and not client.config.session.qobuz.app_id
    await client.login()
    assert client.config.file.qobuz.secrets == [SECRET]
    assert client.config.file.qobuz.app_id == APP_ID and session.request.call_count == 2


async def test_signed_media_errors_hide_request_url(account, tmp_path):
    client, session = account
    client.session = session
    media = client._downloadable({"url": "https://example.invalid/?sig=" + TOKEN}, 2)
    session.head.side_effect = session.get.side_effect = leaking_error()
    for request in (
        media.size(),
        media.download(str(tmp_path / "track"), lambda n: None),
    ):
        with pytest.raises(QobuzRequestError) as caught:
            await request
        assert TOKEN not in "".join(traceback.format_exception(caught.value))
        assert caught.value.__context__ is None


def test_private_config_replacement_preserves_previous_file_on_failure(
    tmp_path, monkeypatch
):
    from streamrip import config

    conf = tmp_path / "config.toml"
    config.set_user_defaults(str(conf))
    original = conf.read_bytes()
    cfg = Config(str(conf))
    cfg.file.qobuz.password_or_token = TOKEN
    cfg.file.set_modified()
    monkeypatch.setattr(
        config.os, "replace", MagicMock(side_effect=OSError("synthetic failure"))
    )
    with pytest.raises(OSError):
        cfg.save_file()
    assert conf.read_bytes() == original and list(tmp_path.iterdir()) == [conf]
    if config.os.name == "posix":
        assert conf.stat().st_mode & 0o077 == 0
