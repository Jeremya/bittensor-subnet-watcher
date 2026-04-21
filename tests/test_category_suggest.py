from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from collectors.github import GitHubCollector


def test_ai_training_keyword():
    from collectors.github import suggest_category

    assert suggest_category(
        "We are training a large language model with distributed gradients"
    ) == "AI Training"


def test_rlhf_keyword():
    from collectors.github import suggest_category

    assert suggest_category(
        "Our subnet implements RLHF alignment fine-tuning"
    ) == "Post-Training/RLHF"


def test_quant_keyword():
    from collectors.github import suggest_category

    assert suggest_category(
        "This subnet provides alpha signals for trading strategies"
    ) == "Quant / Finance"


def test_biomedical_keyword():
    from collectors.github import suggest_category

    assert suggest_category(
        "Protein structure prediction using distributed compute"
    ) == "Biomedical"


def test_data_retrieval_keyword():
    from collectors.github import suggest_category

    assert suggest_category(
        "Decentralized dataset indexing and retrieval network"
    ) == "Data / Retrieval"


def test_infrastructure_keyword():
    from collectors.github import suggest_category

    assert suggest_category(
        "Bandwidth and networking layer for validator communication"
    ) == "Infrastructure"


def test_privacy_keyword():
    from collectors.github import suggest_category

    assert suggest_category(
        "Zero-knowledge proof computation across miners"
    ) == "Privacy / Compute"


def test_unknown_returns_other():
    from collectors.github import suggest_category

    assert suggest_category(
        "This subnet does something vague and undefined"
    ) == "Other"


def test_empty_readme_returns_other():
    from collectors.github import suggest_category

    assert suggest_category("") == "Other"


def test_case_insensitive():
    from collectors.github import suggest_category

    assert suggest_category("TRAINING a model with GRADIENT compression") == "AI Training"


@pytest.mark.asyncio
async def test_collect_adds_category_when_readme_is_available():
    registry = {
        3: {"github_url": "https://github.com/org/repo"},
        8: {"github_url": None},
    }
    repo_data = {
        "gh_last_push": datetime(2026, 4, 21, tzinfo=timezone.utc),
        "gh_stars": 10,
        "gh_forks": 2,
        "gh_open_issues": 1,
    }

    with patch.object(GitHubCollector, "fetch_repo", AsyncMock(return_value=repo_data)), \
            patch("collectors.github.fetch_readme", AsyncMock(return_value="training with llm gradients")):
        result = await GitHubCollector.collect(registry)

    assert 3 in result
    assert result[3]["category"] == "AI Training"
    assert 8 not in result
