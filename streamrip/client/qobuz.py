import asyncio
import base64
import binascii
import hashlib
import logging
import re
import time
from collections import OrderedDict
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from ..config import Config
from ..exceptions import (
    AuthenticationError,
    IneligibleError,
    InvalidAppIdError,
    InvalidAppSecretError,
    MissingCredentialsError,
    NonStreamableError,
    QobuzRequestError,
)
from ..utils.network import aiohttp_call_with_retry
from .client import Client
from .downloadable import Downloadable, QobuzDownloadable

logger = logging.getLogger("streamrip")

QOBUZ_BASE_URL = "https://www.qobuz.com/api.json/0.2"
_PAGINATION_BATCH_SIZE = 10
_REDACTED = "***REDACTED***"
_SENSITIVE_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "email",
        "password",
        "password_or_token",
        "user_id",
        "userid",
        "user_auth_token",
        "request_sig",
        "secret",
        "app_secret",
    }
)

QOBUZ_FEATURED_KEYS = {
    "most-streamed",
    "recent-releases",
    "best-sellers",
    "press-awards",
    "ideal-discography",
    "editor-picks",
    "most-featured",
    "qobuzissims",
    "new-releases",
    "new-releases-full",
    "harmonia-mundi",
    "universal-classic",
    "universal-jazz",
    "universal-jeunesse",
    "universal-chanson",
}


class QobuzSpoofer:
    """Spoofs the information required to stream tracks from Qobuz."""

    def __init__(self, verify_ssl: bool = True):
        """Create a Spoofer."""
        self.seed_timezone_regex = (
            r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.ut'
            r"imezone\.(?P<timezone>[a-z]+)\)"
        )
        # note: {timezones} should be replaced with every capitalized timezone joined by a |
        self.info_extras_regex = (
            r'name:"\w+/(?P<timezone>{timezones})",info:"'
            r'(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'
        )
        self.app_id_regex = (
            r'production:{api:{appId:"(?P<app_id>\d{9})",appSecret:"(\w{32})'
        )
        self.session = None
        self.verify_ssl = verify_ssl

    @staticmethod
    def _extract_bundle_urls(login_page: str) -> list[str]:
        script_paths = re.findall(r'<script[^>]+src="([^"]+)"', login_page)
        normalized = []
        for path in script_paths:
            parsed = urlparse(path)
            script_path = parsed.path
            if "/resources/" not in script_path or not script_path.endswith(".js"):
                continue
            normalized.append(QobuzSpoofer._normalize_script_url(path))

        return normalized

    @staticmethod
    def _normalize_script_url(src: str) -> str:
        if src.startswith("//"):
            parsed = urlparse(f"https:{src}")
        elif src.startswith(("http://", "https://")):
            parsed = urlparse(src)
        else:
            parsed = urlparse(urljoin("https://play.qobuz.com", src))

        return parsed._replace(fragment="").geturl()

    def _extract_app_id_and_secrets_from_bundle(
        self, bundle: str
    ) -> tuple[str, list[str]]:
        match = re.search(self.app_id_regex, bundle)
        if match is None:
            raise ValueError("Could not find app id in Qobuz web assets")

        app_id = str(match.group("app_id"))

        seed_matches = re.finditer(self.seed_timezone_regex, bundle)
        secrets = OrderedDict()
        for seed_match in seed_matches:
            seed, timezone = seed_match.group("seed", "timezone")
            secrets[timezone] = [seed]

        keypairs = list(secrets.items())
        if len(keypairs) < 2:
            raise ValueError("Could not extract enough secret seed/timezone pairs")

        secrets.move_to_end(keypairs[1][0], last=False)

        info_extras_regex = self.info_extras_regex.format(
            timezones="|".join(timezone.capitalize() for timezone in secrets),
        )
        info_extras_matches = re.finditer(info_extras_regex, bundle)
        for info_match in info_extras_matches:
            timezone, info, extras = info_match.group("timezone", "info", "extras")
            secrets[timezone.lower()] += [info, extras]

        for secret_pair in secrets:
            try:
                secrets[secret_pair] = base64.standard_b64decode(
                    "".join(secrets[secret_pair])[:-44],
                ).decode("utf-8")
            except (ValueError, binascii.Error, UnicodeDecodeError):
                secrets[secret_pair] = ""
                continue

        vals: List[str] = list(secrets.values())
        if "" in vals:
            vals.remove("")

        return app_id, vals

    async def get_app_id_and_secrets(self) -> tuple[str, list[str]]:
        """
        Fetch the Qobuz login page, inspect its JavaScript bundle resources, and extract the app id and associated secret seeds.

        Attempts to download the login page, find resource bundle URLs, fetch each bundle (prioritizing non-bundle scripts first), and decode the app id and secrets from the first bundle that yields valid values.

        Returns:
            tuple[str, list[str]]: A pair where the first element is the extracted app id string and the second is a list of decoded secret strings.

        Raises:
            RuntimeError: If the internal HTTP session is not initialized, if no bundle scripts are found on the login page, or if no bundle yields a valid app id and secrets.
        """
        if self.session is None:
            raise RuntimeError("QobuzSpoofer session is not initialized")
        async with self.session.get("https://play.qobuz.com/login") as req:
            login_page = await req.text()

        bundle_urls = self._extract_bundle_urls(login_page)
        if len(bundle_urls) == 0:
            raise RuntimeError(
                "Qobuz login page did not include any resource bundle scripts"
            )

        prioritized = sorted(bundle_urls, key=lambda p: ("bundle" not in p, p))
        for bundle_url in prioritized:
            async with self.session.get(bundle_url) as req:
                bundle = await req.text()
            try:
                app_id, secrets = self._extract_app_id_and_secrets_from_bundle(bundle)
            except ValueError:
                continue
            if app_id and secrets:
                return app_id, secrets

        raise RuntimeError(
            "Unable to extract app id/secrets from current Qobuz web assets"
        )

    async def __aenter__(self):
        from ..utils.ssl_utils import get_aiohttp_connector_kwargs

        # For the spoofer, always use SSL verification
        connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=True)
        connector = aiohttp.TCPConnector(**connector_kwargs)

        self.session = aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=20)
        )
        return self

    async def __aexit__(self, *_):
        if self.session is not None:
            await self.session.close()
        self.session = None


class QobuzClient(Client):
    source = "qobuz"
    max_quality = 4

    def __init__(self, config: Config):
        self.logged_in = False
        self.config = config
        self.rate_limiter = self.get_rate_limiter(
            config.session.downloads.requests_per_minute,
        )
        self.secret: Optional[str] = None
        self._token = ""
        self._login_task: asyncio.Task | None = None
        self._secret_lock = asyncio.Lock()
        self._spoof_cache: tuple[str, list[str]] | None = None

    async def login(self):
        """Share one authentication attempt across concurrent callers."""
        if self.logged_in:
            return
        if self._login_task is None:
            self._login_task = asyncio.create_task(self._login())
        task = self._login_task
        try:
            await task
        finally:
            if self._login_task is task:
                self._login_task = None

    async def _login(self):
        c = self.config.session.qobuz
        if not c.password_or_token or (not c.use_auth_token and not c.email_or_userid):
            raise MissingCredentialsError("Missing Qobuz credentials")
        downloads = self.config.session.downloads
        self.session = await self.get_session(
            verify_ssl=True,
            connect_timeout=downloads.api_connect_timeout_seconds,
            read_timeout=downloads.api_read_timeout_seconds,
        )
        try:
            self._app_id = c.app_id
            if not self._app_id:
                self._app_id, discovered_secrets = await self._get_app_id_and_secrets()

            if c.use_auth_token:
                self._token = c.password_or_token
                _, profile = await self._api_request(
                    "favorite/getUserFavorites", {"type": "albums", "limit": 1}
                )
                albums = profile.get("albums")
                if not isinstance(albums, dict) or not isinstance(
                    albums.get("items"), list
                ):
                    raise QobuzRequestError("Unexpected Qobuz favorites response")
            else:
                _, response = await self._api_request(
                    "user/login",
                    {"email": c.email_or_userid, "password": c.password_or_token},
                    post=True,
                )
                user = response.get("user")
                credential = user.get("credential") if isinstance(user, dict) else None
                if (
                    not isinstance(credential, dict)
                    or not isinstance(response.get("user_auth_token"), str)
                    or not response["user_auth_token"]
                ):
                    raise QobuzRequestError("Unexpected Qobuz login response")
                if not credential.get("parameters"):
                    raise IneligibleError(
                        "Qobuz account is ineligible to stream tracks"
                    )
                self._token = response["user_auth_token"]
            if not c.app_id:
                c.app_id, c.secrets = self._app_id, discovered_secrets
                self._save_app_credentials()
            self.logged_in = True
        finally:
            if not self.logged_in:
                self._token = ""
                await self.session.close()

    def _save_app_credentials(self):
        c = self.config.session.qobuz
        self.config.file.qobuz.app_id = c.app_id
        self.config.file.qobuz.secrets = list(c.secrets)
        self.config.file.set_modified()

    async def get_metadata(self, item: str, media_type: str):
        if media_type == "label":
            return await self.get_label(item)

        c = self.config.session.qobuz
        params = {
            "app_id": str(c.app_id),
            f"{media_type}_id": item,
            # Do these matter?
            "limit": 500,
            "offset": 0,
        }

        extras = {
            "artist": "albums",
            "playlist": "tracks",
            "label": "albums",
        }

        if media_type in extras:
            params.update({"extra": extras[media_type]})

        epoint = f"{media_type}/get"

        status, resp = await self._api_request(epoint, params)

        if status != 200:
            raise NonStreamableError(
                f"Qobuz metadata request failed (HTTP {status})",
            )

        return resp

    async def get_label(self, label_id: str) -> dict:
        """
        Fetches metadata for a Qobuz label and returns the label object with all albums loaded.

        Parameters:
            label_id (str): Qobuz label identifier.

        Returns:
            dict: The label metadata JSON, including an "albums" key whose "items" list contains all albums for the label.

        Raises:
            NonStreamableError: If the initial or any paginated request fails or returns a non-200 status.
        """
        c = self.config.session.qobuz
        page_limit = 500
        params = {
            "app_id": str(c.app_id),
            "label_id": label_id,
            "limit": page_limit,
            "offset": 0,
            "extra": "albums",
        }
        epoint = "label/get"
        status, label_resp = await self._api_request(epoint, params)
        if status != 200:
            raise NonStreamableError(f"Qobuz label request failed (HTTP {status})")
        albums_count = label_resp["albums_count"]

        if albums_count <= page_limit:
            return label_resp

        requests = [
            self._api_request(
                epoint,
                {
                    "app_id": str(c.app_id),
                    "label_id": label_id,
                    "limit": page_limit,
                    "offset": offset,
                    "extra": "albums",
                },
            )
            for offset in range(page_limit, albums_count, page_limit)
        ]

        items = label_resp["albums"]["items"]
        for i in range(0, len(requests), _PAGINATION_BATCH_SIZE):
            batch = requests[i : i + _PAGINATION_BATCH_SIZE]
            results = await asyncio.gather(*batch, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise NonStreamableError(
                        f"Qobuz paginated label request failed ({type(result).__name__})"
                    )
                status, resp = result
                if status != 200:
                    raise NonStreamableError(
                        f"Qobuz paginated label request failed (HTTP {status})"
                    )
                items.extend(resp["albums"]["items"])

        return label_resp

    async def search(self, media_type: str, query: str, limit: int = 500) -> list[dict]:
        if media_type not in ("artist", "album", "track", "playlist"):
            raise Exception(f"{media_type} not available for search on qobuz")

        params = {
            "query": query,
        }
        epoint = f"{media_type}/search"

        return await self._paginate(epoint, params, limit=limit)

    async def get_featured(self, query, limit: int = 500) -> list[dict]:
        params = {
            "type": query,
        }
        if query not in QOBUZ_FEATURED_KEYS:
            raise ValueError(f'query "{query}" is invalid.')
        epoint = "album/getFeatured"
        return await self._paginate(epoint, params, limit=limit)

    async def get_user_favorites(self, media_type: str, limit: int = 500) -> list[dict]:
        if media_type not in ("track", "artist", "album"):
            raise ValueError(f"Unsupported favorites media type: {media_type}")
        params = {"type": f"{media_type}s"}
        epoint = "favorite/getUserFavorites"

        return await self._paginate(epoint, params, limit=limit)

    async def get_user_playlists(self, limit: int = 500) -> list[dict]:
        epoint = "playlist/getUserPlaylists"
        return await self._paginate(epoint, {}, limit=limit)

    async def get_downloadable(self, item: str, quality: int) -> Downloadable:
        if not self.logged_in:
            raise AuthenticationError("Qobuz client is not logged in")
        if not 1 <= quality <= 4:
            raise NonStreamableError(f"Unsupported Qobuz quality level: {quality}")

        if self.secret is None:
            async with self._secret_lock:
                if self.secret is None:
                    return await self._select_secret_and_get_url(item, quality)
        status, result = await self._request_file_url(item, quality, self.secret)
        if status == 400:
            raise NonStreamableError("Qobuz signing failure for track/getFileUrl")
        return self._downloadable(result, quality)

    async def _select_secret_and_get_url(self, item: str, quality: int) -> Downloadable:
        c = self.config.session.qobuz
        candidates = c.secrets
        if not candidates:
            if not c.app_id:
                raise InvalidAppSecretError("Missing validated Qobuz app secret")
            app_id, candidates = await self._get_app_id_and_secrets()
            if str(app_id) != str(c.app_id):
                raise InvalidAppIdError(
                    "Discovered signing credentials use a different Qobuz app ID"
                )
        for secret in dict.fromkeys(candidates):
            status, result = await self._request_file_url(item, quality, secret)
            if status == 400:  # Only an explicit signature rejection reaches here.
                continue
            downloadable = self._downloadable(result, quality)
            self.secret = secret
            if not c.secrets:
                c.secrets = list(candidates)
                self._save_app_credentials()
            return downloadable
        raise InvalidAppSecretError(
            "Qobuz rejected the configured application signing credentials"
        )

    def _downloadable(self, result: dict, quality: int) -> Downloadable:
        url = result.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise NonStreamableError(
                "Qobuz returned no usable HTTPS stream URL at this quality"
            )
        if result.get("sample") in (True, 1, "1", "true"):
            raise IneligibleError("Qobuz returned a preview instead of the full track")
        return QobuzDownloadable(
            self.session, url, "flac" if quality > 1 else "mp3", source="qobuz"
        )

    async def _paginate(
        self,
        epoint: str,
        params: dict,
        limit: int = 500,
    ) -> list[dict]:
        """
        Fetch paginated API pages for the given endpoint until the requested number of items is collected.

        Parameters:
            epoint (str): API endpoint path (e.g. "track/search", "album/getFeatured").
            params (dict): Base query parameters to send with each request; will be copied and updated for pagination.
            limit (int | None): Maximum number of items to consider across pages; if `None`, all available items are fetched.

        Returns:
            list[dict]: A list of page JSON objects returned by the API (the initial page is the first element).

        Raises:
            NonStreamableError: If the initial request or any subsequent page request fails or returns a non-200 status.
        """
        params.update({"limit": limit})
        status, page = await self._api_request(epoint, params)
        if status != 200:
            raise NonStreamableError(
                f"Qobuz pagination request failed ({status}) for {epoint}"
            )
        logger.debug("paginate: initial request made with status %d", status)
        # albums, tracks, etc.
        key = epoint.split("/")[0] + "s"
        items = page.get(key, {})
        total = items.get("total", 0)
        if limit is not None and limit < total:
            total = limit

        logger.debug("paginate: %d total items requested", total)

        if total == 0:
            logger.debug("Nothing found from %s epoint", epoint)
            return []

        limit = int(page.get(key, {}).get("limit", 500))
        offset = int(page.get(key, {}).get("offset", 0))

        logger.debug("paginate: from response: limit=%d, offset=%d", limit, offset)
        params.update({"limit": limit})

        pages = []
        requests = []
        pages.append(page)
        while (offset + limit) < total:
            offset += limit
            params.update({"offset": offset})
            requests.append(self._api_request(epoint, params.copy()))

        for i in range(0, len(requests), _PAGINATION_BATCH_SIZE):
            batch = requests[i : i + _PAGINATION_BATCH_SIZE]
            results = await asyncio.gather(*batch, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise NonStreamableError(
                        f"Qobuz pagination page failed for {epoint}: {type(result).__name__}"
                    )
                status, resp = result
                if status != 200:
                    raise NonStreamableError(
                        f"Qobuz pagination page failed ({status}) for {epoint}"
                    )
                pages.append(resp)

        return pages

    async def _get_app_id_and_secrets(self) -> tuple[str, list[str]]:
        if self._spoof_cache is not None:
            return self._spoof_cache
        async with QobuzSpoofer(
            verify_ssl=self.config.session.downloads.verify_ssl
        ) as spoofer:
            try:
                self._spoof_cache = await spoofer.get_app_id_and_secrets()
                return self._spoof_cache
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                RuntimeError,
                ValueError,
            ):
                pass
        raise InvalidAppIdError(
            "Could not obtain Qobuz application credentials from web assets"
        )

    async def _request_file_url(
        self,
        track_id: str,
        quality: int,
        secret: str,
    ) -> tuple[int, dict]:
        quality = self.get_quality(quality)
        unix_ts = int(time.time())
        r_sig = f"trackgetFileUrlformat_id{quality}intentstreamtrack_id{track_id}{unix_ts}{secret}"
        r_sig_hashed = hashlib.md5(r_sig.encode("utf-8")).hexdigest()
        params = {
            "request_ts": unix_ts,
            "request_sig": r_sig_hashed,
            "track_id": track_id,
            "format_id": quality,
            "intent": "stream",
        }
        return await self._api_request("track/getFileUrl", params)

    async def _api_request(
        self, epoint: str, params: dict, *, post: bool = False
    ) -> tuple[int, dict]:
        """Credentials belong only on this API origin, never on the media session."""
        if not re.fullmatch(r"[a-zA-Z]+/[a-zA-Z]+", epoint):
            raise QobuzRequestError("Invalid Qobuz API endpoint")
        url = f"{QOBUZ_BASE_URL}/{epoint}"
        headers = {"X-App-Id": str(self._app_id)}
        if self._token:
            headers["X-User-Auth-Token"] = self._token
        policy = self.network_retry_policy(self.config.session.downloads)
        transient = {408, 429, 500, 502, 503, 504}

        async def call():
            async with self.rate_limiter:
                async with self.session.request(
                    "POST" if post else "GET",
                    url,
                    headers=headers,
                    allow_redirects=False,
                    **({"data": params} if post else {"params": params}),
                ) as response:
                    status = response.status
                    mime = response.content_type
                    if mime.endswith("+json"):
                        mime = "application/json"
                    if mime not in {"application/json", "text/plain", "text/html"}:
                        mime = "other"
                    body = None
                    if mime == "application/json" and status in {200, 400, 401}:
                        try:
                            body = await response.json()
                        except (ValueError, LookupError, aiohttp.ContentTypeError):
                            pass
                    return status, mime, body

        try:
            status, mime, body = await aiohttp_call_with_retry(
                call,
                operation=f"qobuz_api:{epoint}",
                attempts=1 if post else policy.attempts,
                delay_seconds=policy.delay_seconds,
                backoff=policy.backoff,
                should_retry_result=lambda r: (r[0] in transient, f"http_{r[0]}"),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            # Discard request_info, URLs, nested exceptions and remote messages.
            failure = (
                "timeout"
                if isinstance(exc, asyncio.TimeoutError)
                else type(exc).__name__
            )
        else:
            return self._classify_response(epoint, status, mime, body)
        raise QobuzRequestError(f"Qobuz {epoint}: {failure}")

    @staticmethod
    def _classify_response(epoint, status, mime, body) -> tuple[int, dict]:
        summary = f"Qobuz {epoint} (HTTP {status}, {mime})"
        if status == 200 and isinstance(body, dict):
            return status, body
        message = body.get("message") if isinstance(body, dict) else None
        reason = message.lower() if isinstance(message, str) else ""
        if status in {400, 401} and any(
            part in reason for part in ("app_id", "app id", "application id")
        ):
            raise InvalidAppIdError(f"{summary}: application ID rejected")
        if status == 401 and isinstance(body, dict):
            if "expired" in reason:
                raise AuthenticationError(
                    f"{summary}: credentials expired; renew them in the web player"
                )
            raise AuthenticationError(
                f"{summary}: credentials rejected; check token validity and application ID"
            )
        if (
            status == 400
            and epoint == "track/getFileUrl"
            and ("signature" in reason or "request_sig" in reason)
        ):
            return status, {}
        if status == 403:
            raise QobuzRequestError(
                f"{summary}: access denied; check service or network access"
            )
        if status == 410 and epoint == "track/getFileUrl":
            raise QobuzRequestError(
                f"{summary}: legacy single-file streaming endpoint unavailable"
            )
        if status == 200:
            raise QobuzRequestError(f"{summary}: expected a JSON object")
        raise QobuzRequestError(f"{summary}: request failed")

    @staticmethod
    def _redact_sensitive_mapping(payload: dict) -> dict:
        """Redact sensitive keys in a flat mapping.

        Only top-level keys are redacted; nested dictionaries are not traversed.
        """
        redacted: dict = {}
        for key, value in payload.items():
            if key.lower() in _SENSITIVE_PARAM_KEYS:
                redacted[key] = _REDACTED
            else:
                redacted[key] = value
        return redacted

    @staticmethod
    def _login_response_summary(resp: dict) -> dict:
        user = resp.get("user") or {}
        credential = user.get("credential") or {}
        return {
            "has_user_id": bool(user.get("id")),
            "has_email": bool(user.get("email")),
            "has_user_auth_token": bool(resp.get("user_auth_token")),
            "credential_parameters_present": bool(credential.get("parameters")),
        }

    @staticmethod
    def get_quality(quality: int):
        quality_map = (5, 6, 7, 27)
        return quality_map[quality - 1]
