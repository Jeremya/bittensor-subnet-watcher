import aiohttp
import logging
from datetime import datetime, timezone
from typing import Optional
from utils import aiohttp_session
import config

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com/repos/{owner}/{repo}"


def parse_github_url(url: Optional[str]) -> Optional[tuple[str, str]]:
    """Extract (owner, repo) from a GitHub URL. Returns None if not a GitHub URL."""
    if not url:
        return None
    url = url.rstrip("/")
    if "://github.com/" not in url and not url.startswith("github.com/"):
        return None
    parts = url.split("github.com/", 1)
    if len(parts) < 2:
        return None
    path_parts = parts[1].split("/")
    if len(path_parts) < 2:
        return None
    return path_parts[0], path_parts[1]


class GitHubCollector:
    @staticmethod
    async def fetch_repo(owner: str, repo: str) -> Optional[dict]:
        """
        Fetch repo metadata from GitHub API.
        Returns dict with gh_* keys, or None on 404/error.
        """
        headers = {"Accept": "application/vnd.github.v3+json"}
        if config.GITHUB_TOKEN:
            headers["Authorization"] = f"token {config.GITHUB_TOKEN}"

        url = GITHUB_API.format(owner=owner, repo=repo)
        try:
            async with aiohttp_session() as session:
                async with session.get(
                    url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    pushed_at = None
                    if data.get("pushed_at"):
                        pushed_at = datetime.fromisoformat(
                            data["pushed_at"].replace("Z", "+00:00")
                        )
                    return {
                        "gh_last_push": pushed_at,
                        "gh_stars": data.get("stargazers_count"),
                        "gh_forks": data.get("forks_count"),
                        "gh_open_issues": data.get("open_issues_count"),
                    }
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                logger.info("[COLLECTOR] github: repo_not_found %s/%s", owner, repo)
            elif exc.status == 403:
                logger.warning("[COLLECTOR] github: rate_limited %s/%s", owner, repo)
            else:
                logger.warning("[COLLECTOR] github: http_error %s %s/%s",
                               exc.status, owner, repo)
            return None
        except Exception as exc:
            logger.warning("[COLLECTOR] github: fetch_failed %s/%s error=%s",
                           owner, repo, exc)
            return None

    @staticmethod
    async def collect(registry: dict) -> dict[int, dict]:
        """
        Fetch GitHub data for all subnets in registry that have a github_url.
        Returns {netuid: gh_data_dict}.
        Note: runs sequentially to respect rate limits (60 req/hr unauthenticated).
        """
        results: dict[int, dict] = {}
        for netuid, row in registry.items():
            github_url = row["github_url"] if row["github_url"] else None
            parsed = parse_github_url(github_url)
            if not parsed:
                continue
            owner, repo = parsed
            data = await GitHubCollector.fetch_repo(owner, repo)
            if data is not None:
                results[netuid] = data

        ok = len(results)
        total = sum(1 for r in registry.values() if r["github_url"])
        logger.info("[COLLECTOR] name=github ok=%d errors=%d", ok, total - ok)
        return results
