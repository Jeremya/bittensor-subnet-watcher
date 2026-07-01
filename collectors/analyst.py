import logging
import re

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


def match_subnets(text: str, registry: dict,
                   patterns: list[tuple[int, re.Pattern]] | None = None) -> set[int]:
    """Return set of netuids mentioned in text via SN{n} pattern or subnet name.

    Pass pre-compiled `patterns` (from _name_patterns) when calling in a loop to
    avoid recompiling regexes for every tweet.
    """
    matched: set[int] = set()
    for match in _SN_PATTERN.finditer(text):
        netuid = int(match.group(1))
        if netuid in registry:
            matched.add(netuid)

    if patterns is None:
        patterns = _name_patterns(registry)
    for netuid, pattern in patterns:
        if pattern.search(text):
            matched.add(netuid)
    return matched


# Automated X scraping was retired 2026-07-01 (it never produced data: no
# registry x_handles, and anonymous scraping hits X's login wall). Mentions are
# now hand-curated via engine/mentions.py; match_subnets stays for text matching.
