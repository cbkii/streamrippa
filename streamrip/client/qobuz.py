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
)
from .client import Client
from .downloadable import BasicDownloadable, Downloadable

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

        self.session = aiohttp.ClientSession(connector=connector)
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
        self._spoof_cache: tuple[str, list[str]] | None = None

    async def login(self):
        """User credentials require either a user token OR a user email & password.

        A hash of the password is stored in self.config.qobuz.password_or_token.
        This data as well as the app_id is passed to self._get_user_auth_token() to get
        the actual credentials for the user.
        """
        if self.logged_in:
            raise AuthenticationError("Already logged in to Qobuz in this session")

        c = self.config.session.qobuz
        if not c.email_or_userid or not c.password_or_token:
            raise MissingCredentialsError

        self.session = await self.get_session(
            verify_ssl=self.config.session.downloads.verify_ssl
        )

        if not c.app_id or not c.secrets:
            logger.info("App id/secrets not found, fetching")
            c.app_id, c.secrets = await self._get_app_id_and_secrets()
            # write to file
            f = self.config.file
            f.qobuz.app_id = c.app_id
            f.qobuz.secrets = c.secrets
            f.set_modified()

        self.session.headers.update({"X-App-Id": str(c.app_id)})

        if c.use_auth_token:
            params = {
                "user_id": c.email_or_userid,
                "user_auth_token": c.password_or_token,
                "app_id": str(c.app_id),
            }
        else:
            params = {
                "email": c.email_or_userid,
                "password": c.password_or_token,
                "app_id": str(c.app_id),
            }

        logger.debug("Login request params: %s", self._redact_sensitive_mapping(params))
        status, resp = await self._api_request("user/login", params)
        logger.debug("Login response summary: %s", self._login_response_summary(resp))

        if status == 401:
            raise AuthenticationError("Qobuz rejected credentials or user token")
        if status == 400:
            raise InvalidAppIdError("Qobuz rejected the configured app id")
        if status != 200:
            raise AuthenticationError(
                f"Unexpected Qobuz login response ({status}): {resp.get('message', resp)}"
            )

        logger.debug("Logged in to Qobuz")

        if not resp.get("user", {}).get("credential", {}).get("parameters"):
            raise IneligibleError("Free accounts are not eligible to download tracks.")

        uat = resp["user_auth_token"]
        self.session.headers.update({"X-User-Auth-Token": uat})

        self.secret = await self._get_valid_secret(c.secrets)

        self.logged_in = True

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

        logger.debug(
            "Qobuz metadata request params: %s",
            self._redact_sensitive_mapping(params),
        )

        epoint = f"{media_type}/get"

        status, resp = await self._api_request(epoint, params)

        if status != 200:
            raise NonStreamableError(
                f'Error fetching metadata. Message: "{resp["message"]}"',
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
            raise NonStreamableError(
                f"Error fetching Qobuz label metadata: {label_resp.get('message', label_resp)}"
            )
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
                        f"Error fetching paginated Qobuz label metadata: {result}"
                    )
                status, resp = result
                if status != 200:
                    raise NonStreamableError(
                        f"Error fetching paginated Qobuz label metadata: {resp.get('message', resp)}"
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
        if self.secret is None:
            raise InvalidAppSecretError("Missing validated Qobuz app secret")
        if not 1 <= quality <= 4:
            raise NonStreamableError(f"Unsupported Qobuz quality level: {quality}")

        status, resp_json = await self._request_file_url(item, quality, self.secret)
        if status == 401:
            raise AuthenticationError("Qobuz rejected auth while requesting stream URL")
        if status == 400:
            raise NonStreamableError(
                f"Qobuz signing failure or invalid request for track {item}: {resp_json.get('message', resp_json)}"
            )
        if status != 200:
            raise NonStreamableError(
                f"Qobuz track/getFileUrl failed ({status}) for track {item}: {resp_json.get('message', resp_json)}"
            )
        stream_url = resp_json.get("url")

        if stream_url is None:
            restrictions = resp_json["restrictions"]
            if restrictions:
                # Turn CamelCase code into a readable sentence
                words = re.findall(r"([A-Z][a-z]+)", restrictions[0]["code"])
                raise NonStreamableError(
                    words[0] + " " + " ".join(map(str.lower, words[1:])) + ".",
                )
            raise NonStreamableError(
                f"Qobuz returned no stream URL for track {item}. It may be unavailable/non-streamable at this quality."
            )

        return BasicDownloadable(
            self.session, stream_url, "flac" if quality > 1 else "mp3", source="qobuz"
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
                f"Qobuz pagination request failed ({status}) for {epoint}: {page.get('message', page)}"
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
                        f"Qobuz pagination page failed for {epoint}: {result}"
                    )
                status, resp = result
                if status != 200:
                    raise NonStreamableError(
                        f"Qobuz pagination page failed ({status}) for {epoint}: {resp.get('message', resp)}"
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
            except RuntimeError as e:
                raise InvalidAppIdError(
                    f"Could not extract Qobuz app id/secrets from current web-player assets: {e}"
                )

    async def _test_secret(self, secret: str) -> Optional[str]:
        status, _ = await self._request_file_url("19512574", 4, secret)
        if status == 400:
            return None
        if status == 200 or status == 401:
            return secret
        logger.warning("Got status %d when testing secret", status)
        return None

    async def _get_valid_secret(self, secrets: list[str]) -> str:
        results = await asyncio.gather(
            *[self._test_secret(secret) for secret in secrets],
        )
        working_secrets = [r for r in results if r is not None]
        if len(working_secrets) == 0:
            raise InvalidAppSecretError(secrets)

        return working_secrets[0]

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

    async def _api_request(self, epoint: str, params: dict) -> tuple[int, dict]:
        """Make a request to the API.
        returns: status code, json parsed response
        """
        url = f"{QOBUZ_BASE_URL}/{epoint}"
        logger.debug(
            "api_request: endpoint=%s, params=%s",
            epoint,
            self._redact_sensitive_mapping(params),
        )
        async with self.rate_limiter:
            async with self.session.get(url, params=params) as response:
                return response.status, await response.json()

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
