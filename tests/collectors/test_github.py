import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from collectors.github import GitHubCollector, parse_github_url


def test_parse_github_url_valid():
    owner, repo = parse_github_url("https://github.com/macrocosm-os/prompting")
    assert owner == "macrocosm-os"
    assert repo == "prompting"


def test_parse_github_url_invalid():
    result = parse_github_url("https://notgithub.com/foo/bar")
    assert result is None
    assert parse_github_url("") is None
    assert parse_github_url(None) is None


MOCK_GH_RESPONSE = {
    "pushed_at": "2026-03-28T10:00:00Z",
    "stargazers_count": 142,
    "forks_count": 23,
    "open_issues_count": 7,
}


def make_mock_http_response(data: dict, status: int = 200):
    resp = MagicMock()
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    resp.raise_for_status = MagicMock()
    return resp


async def test_fetch_repo_happy_path():
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_resp = make_mock_http_response(MOCK_GH_RESPONSE)
    mock_session.get = MagicMock(return_value=mock_resp)

    with patch("collectors.github.aiohttp.ClientSession", return_value=mock_session):
        result = await GitHubCollector.fetch_repo("macrocosm-os", "prompting")

    assert result["gh_stars"] == 142
    assert result["gh_forks"] == 23
    assert result["gh_open_issues"] == 7
    assert result["gh_last_push"] is not None


async def test_fetch_repo_404_returns_none_fields():
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_resp = make_mock_http_response({}, status=404)
    import aiohttp
    mock_resp.raise_for_status = MagicMock(
        side_effect=aiohttp.ClientResponseError(MagicMock(), (), status=404))
    mock_session.get = MagicMock(return_value=mock_resp)

    with patch("collectors.github.aiohttp.ClientSession", return_value=mock_session):
        result = await GitHubCollector.fetch_repo("org", "deleted-repo")

    assert result is None
