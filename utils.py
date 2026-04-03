import asyncio
import logging
import ssl
import certifi
import aiohttp
from typing import Callable, TypeVar, Awaitable

logger = logging.getLogger(__name__)


def ssl_context() -> ssl.SSLContext:
    """Return an SSL context using certifi's CA bundle."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    return ctx


def aiohttp_session(**kwargs) -> aiohttp.ClientSession:
    """Return an aiohttp ClientSession with certifi SSL context."""
    connector = aiohttp.TCPConnector(ssl=ssl_context())
    return aiohttp.ClientSession(connector=connector, **kwargs)
T = TypeVar("T")


async def async_retry(
    fn: Callable[[], Awaitable[T]],
    retries: int = 1,
    delay: float = 5.0,
) -> T:
    """Call an async function, retrying up to `retries` times on any exception."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                logger.warning("Attempt %d failed (%s), retrying in %.1fs",
                               attempt + 1, exc, delay)
                if delay > 0:
                    await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
