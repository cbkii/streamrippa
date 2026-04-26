"""Network retry helpers for transient aiohttp failures."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import aiohttp

logger = logging.getLogger("streamrip")

T = TypeVar("T")

_RETRYABLE_CLIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    aiohttp.ClientConnectionError,
    aiohttp.ClientOSError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientPayloadError,
)
_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})


def _coerce_retry_decision(decision: bool | tuple[bool, str]) -> tuple[bool, str]:
    if isinstance(decision, tuple):
        retry, reason = decision
        return retry, reason
    return decision, "result_retry"


async def aiohttp_call_with_retry(
    call_factory: Callable[[], Awaitable[T]],
    *,
    operation: str,
    attempts: int,
    delay_seconds: float,
    backoff: float = 2.0,
    should_retry_result: Callable[[T], bool | tuple[bool, str]] | None = None,
) -> T:
    """Run ``call_factory`` with bounded retries for transient aiohttp failures.

    This helper intentionally retries only transient transport/time-out conditions.
    It does not hard-exit any run; it logs retry telemetry and either returns the
    final result or re-raises the terminal exception for caller-level handling.
    """
    safe_attempts = max(1, int(attempts))
    safe_delay = max(0.0, float(delay_seconds))
    safe_backoff = max(1.0, float(backoff))
    current_delay = safe_delay

    for attempt in range(1, safe_attempts + 1):
        try:
            result = await call_factory()
        except aiohttp.ClientResponseError as exc:
            status = int(getattr(exc, "status", 0) or 0)
            is_retryable_status = status in _RETRYABLE_HTTP_STATUSES
            if attempt >= safe_attempts or not is_retryable_status:
                logger.warning(
                    "network_retry_terminal op=%s reason=http_%s attempt=%d/%d error=%s",
                    operation,
                    status or "unknown",
                    attempt,
                    safe_attempts,
                    exc,
                )
                raise
            logger.warning(
                "network_retry op=%s reason=http_%s attempt=%d/%d sleep=%.2fs error=%s",
                operation,
                status,
                attempt,
                safe_attempts,
                current_delay,
                exc,
            )
            await asyncio.sleep(current_delay)
            current_delay *= safe_backoff
            continue
        except _RETRYABLE_CLIENT_EXCEPTIONS as exc:
            reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else "transport"
            if attempt >= safe_attempts:
                logger.warning(
                    "network_retry_terminal op=%s reason=%s attempt=%d/%d error=%s",
                    operation,
                    reason,
                    attempt,
                    safe_attempts,
                    exc,
                )
                raise
            logger.warning(
                "network_retry op=%s reason=%s attempt=%d/%d sleep=%.2fs error=%s",
                operation,
                reason,
                attempt,
                safe_attempts,
                current_delay,
                exc,
            )
            await asyncio.sleep(current_delay)
            current_delay *= safe_backoff
            continue

        if should_retry_result is None:
            return result

        retry, reason = _coerce_retry_decision(should_retry_result(result))
        if not retry:
            return result
        if attempt >= safe_attempts:
            logger.warning(
                "network_retry_result_terminal op=%s reason=%s attempt=%d/%d",
                operation,
                reason,
                attempt,
                safe_attempts,
            )
            return result
        logger.warning(
            "network_retry_result op=%s reason=%s attempt=%d/%d sleep=%.2fs",
            operation,
            reason,
            attempt,
            safe_attempts,
            current_delay,
        )
        await asyncio.sleep(current_delay)
        current_delay *= safe_backoff

    # Unreachable because safe_attempts >= 1 and every branch returns/raises.
    raise RuntimeError("aiohttp_call_with_retry exhausted without result")
