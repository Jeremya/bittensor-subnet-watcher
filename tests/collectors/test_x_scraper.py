import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from collectors.x_scraper import XCollector, parse_follower_count


def test_parse_follower_count():
    assert parse_follower_count("1,234 Followers") == 1234
    assert parse_follower_count("12.3K Followers") == 12300
    assert parse_follower_count("2.1M Followers") == 2100000
    assert parse_follower_count("") is None
    assert parse_follower_count(None) is None
    assert parse_follower_count("No followers info") is None


async def test_scrape_handle_happy_path():
    mock_page = MagicMock()
    mock_page.goto = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()
    mock_page.query_selector = AsyncMock()

    # Mock follower element
    follower_el = MagicMock()
    follower_el.text_content = AsyncMock(return_value="5,432 Followers")

    # Mock latest tweet time element
    tweet_el = MagicMock()
    tweet_el.get_attribute = AsyncMock(return_value="2026-03-30T10:00:00.000Z")

    mock_page.query_selector = AsyncMock(side_effect=[follower_el, tweet_el])

    with patch("collectors.x_scraper.get_browser_page",
               AsyncMock(return_value=mock_page)):
        result = await XCollector.scrape_handle("actualinc")

    assert result["x_followers"] == 5432
    assert result["x_last_tweet"] is not None


async def test_scrape_handle_returns_none_on_timeout():
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    mock_page = MagicMock()
    mock_page.goto = AsyncMock(side_effect=PlaywrightTimeout("timeout"))

    with patch("collectors.x_scraper.get_browser_page",
               AsyncMock(return_value=mock_page)):
        result = await XCollector.scrape_handle("somehandle")

    assert result is None


async def test_collect_respects_max_per_cycle():
    registry = {i: {"x_handle": f"handle{i}"} for i in range(50)}
    with patch("collectors.x_scraper.XCollector.scrape_handle",
               AsyncMock(return_value={"x_followers": 100, "x_last_tweet": None})):
        with patch("collectors.x_scraper.asyncio.sleep", AsyncMock()):
            results = await XCollector.collect(registry, max_per_cycle=30)
    assert len(results) == 30
