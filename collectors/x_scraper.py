import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout
import config

logger = logging.getLogger(__name__)

_playwright = None
_browser = None


def _row_value(row, key: str):
    """Read a registry value from dict-like rows or sqlite Row objects."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, None)


async def get_browser_page() -> Page:
    """Get or create a headless Chromium page."""
    global _playwright, _browser
    if _browser is None:
        _playwright = await asyncio.wait_for(async_playwright().start(), timeout=30)
        _browser = await asyncio.wait_for(
            _playwright.chromium.launch(headless=True), timeout=30
        )
    context = await _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    )
    return await context.new_page()


async def close_browser() -> None:
    global _playwright, _browser
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


def parse_follower_count(text: Optional[str]) -> Optional[int]:
    """Parse '5,432 Followers', '12.3K Followers', '2.1M Followers' → int."""
    if not text:
        return None
    text = text.strip()
    # Extract number part
    m = re.search(r"([\d,]+\.?\d*)\s*([KkMm]?)", text)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    multiplier = m.group(2).upper()
    try:
        val = float(num_str)
        if multiplier == "K":
            val *= 1_000
        elif multiplier == "M":
            val *= 1_000_000
        return int(val)
    except ValueError:
        return None


class XCollector:
    @staticmethod
    async def scrape_handle(handle: str) -> Optional[dict]:
        """
        Scrape follower count and latest tweet date for an X handle.
        Returns None silently on any failure.
        """
        page = None
        try:
            page = await get_browser_page()
            await page.goto(f"https://x.com/{handle}",
                            wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_selector('[data-testid="primaryColumn"]', timeout=10_000)

            # Follower count
            x_followers: Optional[int] = None
            follower_el = await page.query_selector('a[href$="/verified_followers"] span, '
                                                    'a[href$="/followers"] span')
            if follower_el:
                text = await follower_el.text_content()
                x_followers = parse_follower_count(text)

            # Latest tweet time
            x_last_tweet: Optional[datetime] = None
            time_el = await page.query_selector('article time')
            if time_el:
                dt_str = await time_el.get_attribute("datetime")
                if dt_str:
                    x_last_tweet = datetime.fromisoformat(
                        dt_str.replace("Z", "+00:00")
                    )

            return {"x_followers": x_followers, "x_last_tweet": x_last_tweet}

        except asyncio.CancelledError:
            raise
        except PlaywrightTimeout:
            logger.warning("[COLLECTOR] x_scraper: timeout handle=%s", handle)
            return None
        except Exception as exc:
            logger.warning("[COLLECTOR] x_scraper: failed handle=%s error=%s", handle, exc)
            return None
        finally:
            if page:
                try:
                    await page.context.close()
                except Exception:
                    pass

    @staticmethod
    async def collect(registry: dict,
                      max_per_cycle: int = config.X_SCRAPE_MAX_PER_CYCLE) -> dict[int, dict]:
        """
        Scrape X handles for up to max_per_cycle subnets per run.
        Sequential with 2s delay to avoid IP bans.
        """
        results: dict[int, dict] = {}
        handles = [
            (netuid, handle)
            for netuid, row in registry.items()
            if (handle := _row_value(row, "x_handle"))
        ][:max_per_cycle]

        for netuid, handle in handles:
            data = await XCollector.scrape_handle(handle)
            if data is not None:
                results[netuid] = data
            await asyncio.sleep(config.X_SCRAPE_DELAY_SECONDS)

        ok = len(results)
        logger.info("[COLLECTOR] name=x ok=%d errors=%d (best-effort)",
                    ok, len(handles) - ok)
        return results
