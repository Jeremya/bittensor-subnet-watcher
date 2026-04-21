import asyncio
import logging
import re
from datetime import datetime, timezone

import aiosqlite

import config
from collectors.x_scraper import get_browser_page

logger = logging.getLogger(__name__)

_SN_PATTERN = re.compile(r"\bSN(\d+)\b", re.IGNORECASE)


def _registry_name(row) -> str | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get("name")
    try:
        return row["name"]
    except (KeyError, TypeError, IndexError):
        return getattr(row, "name", None)


def _name_patterns(registry: dict) -> list[tuple[int, re.Pattern]]:
    patterns: list[tuple[int, re.Pattern]] = []
    for netuid, row in registry.items():
        name = _registry_name(row)
        if name:
            patterns.append(
                (netuid, re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE))
            )
    return patterns


def match_subnets(text: str, registry: dict) -> set[int]:
    matched: set[int] = set()
    for match in _SN_PATTERN.finditer(text):
        netuid = int(match.group(1))
        if netuid in registry:
            matched.add(netuid)

    for netuid, pattern in _name_patterns(registry):
        if pattern.search(text):
            matched.add(netuid)
    return matched


async def _scrape_tweets(handle: str, lookback_hours: int) -> list[dict]:
    page = None
    tweets: list[dict] = []
    cutoff_ts = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    try:
        page = await get_browser_page()
        await page.goto(
            f"https://x.com/{handle}",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        await page.wait_for_selector('[data-testid="primaryColumn"]', timeout=10_000)

        articles = await page.query_selector_all("article")
        for article in articles[:10]:
            time_el = await article.query_selector("time")
            if not time_el:
                continue

            dt_str = await time_el.get_attribute("datetime")
            if not dt_str:
                continue
            posted_at = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if posted_at.timestamp() < cutoff_ts:
                continue

            text_el = await article.query_selector('[data-testid="tweetText"]')
            text = await text_el.text_content() if text_el else ""

            link_el = await time_el.evaluate_handle("el => el.closest('a')")
            href = await link_el.get_attribute("href") if link_el else None
            url = f"https://x.com{href}" if href and href.startswith("/") else href

            if url and text:
                tweets.append({"url": url, "text": text, "posted_at": posted_at})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[COLLECTOR] analyst: handle=%s error=%s", handle, exc)
    finally:
        if page:
            try:
                await page.context.close()
            except Exception:
                pass
    return tweets


class AnalystCollector:
    @staticmethod
    async def _all_handles(db: aiosqlite.Connection) -> list[str]:
        from db.database import get_analyst_watchlist

        db_rows = await get_analyst_watchlist(db)
        db_handles = {row["handle"].lstrip("@") for row in db_rows}
        config_handles = {handle.lstrip("@") for handle in config.ANALYST_HANDLES}
        return sorted(config_handles | db_handles)

    @staticmethod
    async def collect(db: aiosqlite.Connection, registry: dict) -> int:
        from db.database import insert_analyst_mention

        handles = await AnalystCollector._all_handles(db)
        if not handles:
            logger.info("[COLLECTOR] name=analyst no_handles_configured")
            return 0

        new_count = 0
        for handle in handles:
            tweets = await _scrape_tweets(handle, config.ANALYST_TWEET_LOOKBACK_HOURS)
            for tweet in tweets:
                for netuid in match_subnets(tweet["text"], registry):
                    inserted = await insert_analyst_mention(
                        db,
                        handle,
                        netuid,
                        tweet["url"],
                        tweet["text"],
                        tweet["posted_at"],
                    )
                    if inserted:
                        new_count += 1
            await asyncio.sleep(2.0)

        logger.info(
            "[COLLECTOR] name=analyst new_mentions=%d handles=%d",
            new_count,
            len(handles),
        )
        return new_count
