"""
mentions.py — Build a per-entity mention index from EPUB text.

For each character, location, term, and reference in an X-Ray, find every
chapter that mentions it by name. Stores a compact index alongside the X-Ray
in the cache:

  mentions[entity_name] = [
    { "chapter": "6: The Martyr", "position_pct": 11, "snippet": "...Eo is hanged..." },
    ...
  ]

Called automatically after X-Ray generation. Can be re-run standalone.
"""

import json
import logging
import re
from pathlib import Path

from epub_extract import EpubContent, Chapter

logger = logging.getLogger(__name__)

# Minimum name length to bother searching (avoids "I", "a", etc.)
MIN_NAME_LEN = 3
# Max snippet chars around a match
SNIPPET_RADIUS = 60
# Max mentions to store per entity (keep the most spread-out ones)
MAX_MENTIONS = 30


def _search_names(entity: dict, name_key: str = "name",
                  alias_key: str = "aliases") -> list[str]:
    """Return all searchable names for an entity (primary + components + aliases)."""
    names = []
    primary = entity.get(name_key, "")
    if len(primary) >= MIN_NAME_LEN:
        names.append(primary)
        words = primary.split()
        if len(words) > 1:
            # First word — the common given-name reference ("Sevro" from "Sevro au Fitchner")
            if len(words[0]) >= MIN_NAME_LEN:
                names.append(words[0])
            # Last word — family/house name
            if len(words[-1]) >= MIN_NAME_LEN and words[-1].lower() != words[0].lower():
                names.append(words[-1])

    for alias in entity.get(alias_key) or []:
        if alias and len(alias) >= MIN_NAME_LEN:
            names.append(alias)

    # Deduplicate, keep order
    seen = set()
    result = []
    for n in names:
        nl = n.lower()
        if nl not in seen:
            seen.add(nl)
            result.append(n)
    return result


def _find_in_chapter(chapter: Chapter, search_names: list[str]) -> dict | None:
    """Return a mention entry if any search_name appears in the chapter text."""
    text = chapter.text
    text_lower = text.lower()

    for name in search_names:
        nl = name.lower()
        pos = text_lower.find(nl)
        if pos == -1:
            continue
        # Build snippet
        start = max(0, pos - SNIPPET_RADIUS)
        end   = min(len(text), pos + len(name) + SNIPPET_RADIUS)
        snippet = text[start:end].replace("\n", " ").replace("\r", "")
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
        return {
            "chapter":      chapter.title,
            "position_pct": round(chapter.position_pct),
            "snippet":      snippet[:250],
        }
    return None


def _spread_mentions(mentions: list[dict], max_count: int) -> list[dict]:
    """If we have too many mentions, keep a spread-out sample."""
    if len(mentions) <= max_count:
        return mentions
    # Take evenly-spaced indices
    step = len(mentions) / max_count
    indices = {round(i * step) for i in range(max_count)}
    return [m for i, m in enumerate(mentions) if i in indices]


def build_mentions(content: EpubContent, xray: dict) -> dict:
    """
    Scan the full EPUB chapter by chapter for each X-Ray entity.

    Returns a dict:  { entity_name_lower: [mention, ...] }

    Caller should merge this into the cache record under key "mentions".
    """
    # Collect all entities to search
    entities: list[tuple[str, dict, list[str]]] = []  # (category, entity, search_names)

    for cat in ("characters", "locations", "terms", "references", "historical_figures"):
        alias_key = "aliases" if cat in ("characters", "terms", "references") else None
        for entity in xray.get(cat, []):
            names = _search_names(entity, alias_key=alias_key)
            if names:
                entities.append((cat, entity, names))

    logger.info("mentions: scanning %d chapters for %d entities",
                len(content.chapters), len(entities))

    # For each entity, collect chapter-level mentions
    result: dict[str, list[dict]] = {}

    for _cat, entity, search_names in entities:
        primary_name = entity.get("name", "")
        key          = primary_name.lower()
        mentions     = []

        for chapter in content.chapters:
            hit = _find_in_chapter(chapter, search_names)
            if hit:
                mentions.append(hit)

        if mentions:
            result[key] = _spread_mentions(mentions, MAX_MENTIONS)

    total_mentions = sum(len(v) for v in result.values())
    logger.info("mentions: found %d mention entries across %d entities",
                total_mentions, len(result))

    return result


def add_mention_counts(xray: dict, mentions: dict) -> dict:
    """
    Annotate each entity in the xray with a chapter_count field.
    Modifies xray in-place and returns it.
    """
    for cat in ("characters", "locations", "terms", "references", "historical_figures"):
        for entity in xray.get(cat, []):
            key   = (entity.get("name") or "").lower()
            count = len(mentions.get(key, []))
            if count:
                entity["chapter_count"] = count
    return xray
