"""The clients that interact with the streaming service APIs."""

import contextlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import aiohttp
import aiolimiter

from ..utils.ssl_utils import get_aiohttp_connector_kwargs
from .downloadable import Downloadable

logger = logging.getLogger("streamrip")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0"
)


@dataclass(slots=True, frozen=True)
class NetworkRetryPolicy:
    attempts: int
    delay_seconds: float
    backoff: float = 2.0


class Client(ABC):
    source: str
    max_quality: int
    session: aiohttp.ClientSession
    logged_in: bool

    @abstractmethod
    async def login(self):
        raise NotImplementedError

    @abstractmethod
    async def get_metadata(self, item: str, media_type):
        raise NotImplementedError

    @abstractmethod
    async def search(self, media_type: str, query: str, limit: int = 500) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    async def get_downloadable(self, item: str, quality: int) -> Downloadable:
        raise NotImplementedError

    @staticmethod
    def get_rate_limiter(
        requests_per_min: int,
    ) -> aiolimiter.AsyncLimiter | contextlib.nullcontext:
        return (
            aiolimiter.AsyncLimiter(requests_per_min, 60)
            if requests_per_min > 0
            else contextlib.nullcontext()
        )

    @staticmethod
    async def get_session(
        headers: dict | None = None,
        verify_ssl: bool = True,
        connect_timeout: float = 15.0,
        read_timeout: float = 120.0,
    ) -> aiohttp.ClientSession:
        if headers is None:
            headers = {}

        # Get connector kwargs based on SSL verification setting
        connector_kwargs = get_aiohttp_connector_kwargs(verify_ssl=verify_ssl)
        connector = aiohttp.TCPConnector(**connector_kwargs)
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=max(0.1, float(connect_timeout)),
            sock_read=max(1.0, float(read_timeout)),
        )

        return aiohttp.ClientSession(
            headers={"User-Agent": DEFAULT_USER_AGENT} | headers,
            connector=connector,
            timeout=timeout,
        )

    @staticmethod
    def network_retry_policy(downloads_cfg) -> NetworkRetryPolicy:
        return NetworkRetryPolicy(
            attempts=max(1, int(getattr(downloads_cfg, "api_request_retries", 2))),
            delay_seconds=max(
                0.0, float(getattr(downloads_cfg, "api_retry_delay_seconds", 0.75))
            ),
        )
