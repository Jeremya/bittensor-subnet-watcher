import asyncio
import logging
from typing import Callable, TypeVar, Awaitable

logger = logging.getLogger(__name__)
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
