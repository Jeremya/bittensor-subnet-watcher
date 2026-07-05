import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import aiosqlite

import config
from collectors.github import parse_github_url
from utils import aiohttp_session

logger = logging.getLogger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"
GITHUB_RELEASES_API = "https://api.github.com/repos/{owner}/{repo}/releases"
_FIRST_RUN_LOOKBACK_DAYS = 7


class _RateLimited(Exception):
    """GitHub returned 403: abort the releases source for this run."""
_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
_VERSION_RE = re.compile(r"v\d+$")

# Module-level async client — created once if API key is set, avoids repeated init overhead
_anthropic_client = None
if config.ANTHROPIC_API_KEY:
    try:
        import anthropic as _anthropic_module
        _anthropic_client = _anthropic_module.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    except Exception:
        pass


def parse_arxiv_feed(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries: list[dict] = []
    for entry in root.findall("atom:entry", _ARXIV_NS):
        title_el = entry.find("atom:title", _ARXIV_NS)
        id_el = entry.find("atom:id", _ARXIV_NS)
        pub_el = entry.find("atom:published", _ARXIV_NS)
        if title_el is None or id_el is None or pub_el is None:
            continue

        raw_id = (id_el.text or "").strip()
        title = (title_el.text or "").strip()
        published = (pub_el.text or "").strip()
        if not raw_id or not title or not published:
            continue

        url = _VERSION_RE.sub("", raw_id).replace(
            "http://arxiv.org/abs/",
            "https://arxiv.org/abs/",
        )
        try:
            published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue

        entries.append({
            "title": title,
            "url": url,
            "published_at": published_at,
        })
    return entries


def parse_release_entries(payload: list[dict],
                          since_iso: Optional[str]) -> list[dict]:
    """Filter raw GitHub /releases JSON into milestone entries.

    Drafts and prereleases are noise. On the first run (no since state) only
    the last 7 days are ingested — never a 109-repo backlog flood.
    """
    if since_iso is None:
        since_iso = (datetime.now(timezone.utc)
                     - timedelta(days=_FIRST_RUN_LOOKBACK_DAYS)).isoformat()

    entries: list[dict] = []
    for release in payload:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        url = release.get("html_url")
        published_raw = release.get("published_at")
        if not tag or not url or not published_raw:
            continue
        try:
            published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published_at.isoformat() <= since_iso:
            continue
        name = release.get("name") or tag
        entries.append({
            "title": f"{tag} — {name}",
            "url": url,
            "published_at": published_at,
        })
    return entries


async def interpret_milestone(subnet_name: str,
                              netuid: int,
                              milestone_type: str,
                              title: str,
                              url: str) -> tuple[Optional[str], Optional[str]]:
    if _anthropic_client is None:
        return None, None

    try:
        prompt = (
            "You are a Bittensor investment analyst. Given a new publication from a "
            "Bittensor subnet team, write two things:\n"
            "1. SUMMARY: 1-2 sentences explaining what was published in plain English "
            "for a non-technical investor.\n"
            "2. TAKE: 1 sentence on what this means for the subnet's investment thesis.\n\n"
            f"Subnet: {subnet_name} (SN{netuid})\n"
            f"Publication type: {milestone_type}\n"
            f"Title: {title}\n"
            f"URL: {url}\n\n"
            'Reply in JSON only: {"summary": "...", "take": "..."}'
        )
        response = await _anthropic_client.messages.create(
            model=config.AI_INTERPRETER_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        payload = json.loads(response.content[0].text)
        return payload.get("summary"), payload.get("take")
    except Exception as exc:
        logger.warning("[COLLECTOR] milestone: AI interpret failed title=%r error=%s", title, exc)
        return None, None


class MilestoneCollector:
    @staticmethod
    async def _query_arxiv(subnet_name: str, since_iso: Optional[str]) -> list[dict]:
        query = f'all:"bittensor" AND all:"{subnet_name}"'
        params = {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": "5",
        }
        try:
            async with aiohttp_session() as session:
                async with session.get(
                    ARXIV_API,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        return []
                    text = await resp.text()
        except Exception as exc:
            logger.warning("[COLLECTOR] milestone: arxiv failed subnet=%r error=%s", subnet_name, exc)
            return []

        entries = parse_arxiv_feed(text)
        if since_iso:
            entries = [entry for entry in entries if entry["published_at"].isoformat() > since_iso]
        return entries

    @staticmethod
    async def _query_huggingface(subnet_name: str, since_iso: Optional[str]) -> list[dict]:
        try:
            from huggingface_hub import HfApi

            api = HfApi()
            models = await asyncio.to_thread(
                lambda: list(api.list_models(
                    search=f"{subnet_name} bittensor",
                    limit=5,
                    sort="lastModified",
                    direction=-1,
                ))
            )
        except Exception as exc:
            logger.warning("[COLLECTOR] milestone: hf failed subnet=%r error=%s", subnet_name, exc)
            return []

        results: list[dict] = []
        for model in models:
            last_modified = model.lastModified
            if last_modified is None:
                continue
            if isinstance(last_modified, str):
                try:
                    last_modified = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=timezone.utc)
            if since_iso and last_modified.isoformat() <= since_iso:
                continue
            results.append({
                "title": model.id,
                "url": f"https://huggingface.co/{model.id}",
                "published_at": last_modified,
            })
        return results

    @staticmethod
    async def _query_github_releases(owner: str, repo: str,
                                     since_iso: Optional[str]) -> list[dict]:
        headers = {"Accept": "application/vnd.github+json"}
        if config.GITHUB_TOKEN:
            headers["Authorization"] = f"token {config.GITHUB_TOKEN}"
        url = GITHUB_RELEASES_API.format(owner=owner, repo=repo)
        try:
            async with aiohttp_session() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params={"per_page": "5"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 403:
                        raise _RateLimited(url)
                    if resp.status != 200:
                        return []
                    payload = await resp.json()
        except _RateLimited:
            raise
        except Exception as exc:
            logger.warning("[COLLECTOR] milestone: releases failed repo=%s/%s error=%s",
                           owner, repo, exc)
            return []
        if not isinstance(payload, list):
            return []
        return parse_release_entries(payload, since_iso)

    @staticmethod
    async def collect(db: aiosqlite.Connection, registry: dict) -> int:
        from db.database import get_collector_state, insert_milestone, set_collector_state

        arxiv_since = await get_collector_state(db, "milestone_last_arxiv_check")
        hf_since = await get_collector_state(db, "milestone_last_hf_check")
        github_since = await get_collector_state(db, "milestone_last_github_check")
        releases_enabled = True
        now_iso = datetime.now(timezone.utc).isoformat()

        new_count = 0
        subnets_with_repo = [
            (netuid, row)
            for netuid, row in registry.items()
            if (row["github_url"] if isinstance(row, dict) else row["github_url"])
        ]

        for netuid, row in subnets_with_repo:
            name = (row["name"] if isinstance(row, dict) else row["name"]) or f"SN{netuid}"

            for entry in await MilestoneCollector._query_arxiv(name, arxiv_since):
                summary, take = await interpret_milestone(
                    name, netuid, "arxiv", entry["title"], entry["url"]
                )
                inserted = await insert_milestone(
                    db,
                    netuid,
                    "arxiv",
                    entry["title"],
                    entry["url"],
                    entry["published_at"],
                    ai_summary=summary,
                    ai_take=take,
                )
                if inserted:
                    new_count += 1

            for entry in await MilestoneCollector._query_huggingface(name, hf_since):
                summary, take = await interpret_milestone(
                    name, netuid, "huggingface", entry["title"], entry["url"]
                )
                inserted = await insert_milestone(
                    db,
                    netuid,
                    "huggingface",
                    entry["title"],
                    entry["url"],
                    entry["published_at"],
                    ai_summary=summary,
                    ai_take=take,
                )
                if inserted:
                    new_count += 1

            repo_parts = parse_github_url(
                row.get("github_url") if isinstance(row, dict) else row["github_url"])
            if releases_enabled and repo_parts:
                try:
                    release_entries = await MilestoneCollector._query_github_releases(
                        repo_parts[0], repo_parts[1], github_since)
                except _RateLimited:
                    logger.warning("[COLLECTOR] milestone: github rate-limited — "
                                   "skipping releases for the rest of this run")
                    releases_enabled = False
                    release_entries = []
                for entry in release_entries:
                    summary, take = await interpret_milestone(
                        name, netuid, "github_release", entry["title"], entry["url"])
                    inserted = await insert_milestone(
                        db,
                        netuid,
                        "github_release",
                        entry["title"],
                        entry["url"],
                        entry["published_at"],
                        ai_summary=summary,
                        ai_take=take,
                    )
                    if inserted:
                        new_count += 1

            await asyncio.sleep(1.0)

        await set_collector_state(db, "milestone_last_arxiv_check", now_iso)
        await set_collector_state(db, "milestone_last_hf_check", now_iso)
        await set_collector_state(db, "milestone_last_github_check", now_iso)
        logger.info(
            "[COLLECTOR] name=milestone new=%d subnets_checked=%d",
            new_count,
            len(subnets_with_repo),
        )
        return new_count
