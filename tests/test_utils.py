import pytest
from utils import async_retry


async def test_async_retry_succeeds_on_first_try():
    calls = []
    async def ok():
        calls.append(1)
        return "result"
    result = await async_retry(ok)
    assert result == "result"
    assert len(calls) == 1


async def test_async_retry_retries_once_on_failure():
    calls = []
    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise ValueError("first fail")
        return "ok"
    result = await async_retry(flaky, retries=1, delay=0)
    assert result == "ok"
    assert len(calls) == 2


async def test_async_retry_raises_after_max_retries():
    async def always_fails():
        raise RuntimeError("always")
    with pytest.raises(RuntimeError, match="always"):
        await async_retry(always_fails, retries=1, delay=0)
