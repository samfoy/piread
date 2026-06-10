"""
series.py — Cross-book series context for X-Ray.

When opening book N in a series, pull characters/locations/terms from
books 1..N-1 whose X-Rays are already cached. Annotates inherited
entities with source_book so the UI can show "From Red Rising #1".

Called from xray_generator.build_record() when series info is present.
"""

import logging
from copy import deepcopy

import xray_cache

logger = logging.getLogger(__name__)


def _norm(name: str) -> str:
    return name.lower().strip() if name else ""


def _entity_key(entity: dict, name_key: str = "name") -> str:
    return _norm(entity.get(name_key, ""))


def _merge_from_prior(
    current: list[dict],
    prior: list[dict],
    desc_key: str,
    source_label: str,
) -> list[dict]:
    """
    Merge prior-book entities into current, tagging them source_book=source_label.
    Existing entities are NOT overwritten — prior knowledge is supplemental.
    Characters/locations already in current are enriched with prior description
    only if they have none.
    """
    current_keys = {_entity_key(e) for e in current}
    added = 0
    enriched = 0

    for prior_entity in prior:
        key = _entity_key(prior_entity)
        if not key:
            continue

        if key in current_keys:
            # Already known — optionally enrich if current has no description
            for e in current:
                if _entity_key(e) == key:
                    if not e.get(desc_key) and prior_entity.get(desc_key):
                        e[desc_key] = f"[From {source_label}] {prior_entity[desc_key]}"
                        enriched += 1
                    break
        else:
            # Not yet known — import with source tag
            copy = deepcopy(prior_entity)
            copy["source_book"]  = source_label
            copy["source_label"] = f"From {source_label}"
            current.append(copy)
            current_keys.add(key)
            added += 1

    if added or enriched:
        logger.info("series: %s → added %d, enriched %d", source_label, added, enriched)
    return current


def inject_series_context(xray: dict, series: str, series_index: int) -> dict:
    """
    Pull entities from prior books in the same series that are already cached.

    Mutates xray in place (adds/enriches characters, locations, terms from
    earlier books). Returns the modified xray.
    """
    if not series or not series_index or series_index <= 1:
        return xray

    prior_records = xray_cache.get_series(series)
    # Filter to books with a lower index than current
    prior_books = [
        r for r in prior_records
        if r.get("book", {}).get("series_index", 0) < series_index
    ]

    if not prior_books:
        logger.info("series: no prior books cached for '%s'", series)
        return xray

    logger.info(
        "series: injecting context from %d prior book(s) of '%s'",
        len(prior_books), series
    )

    for record in sorted(prior_books, key=lambda r: r.get("book", {}).get("series_index", 0)):
        book_meta = record.get("book", {})
        idx   = book_meta.get("series_index", "?")
        title = book_meta.get("title", f"Book {idx}")
        label = f"{series} #{idx} ({title})"
        prior = record.get("xray", {})

        xray["characters"] = _merge_from_prior(
            xray.get("characters", []),
            prior.get("characters", []),
            "description", label,
        )
        xray["locations"] = _merge_from_prior(
            xray.get("locations", []),
            prior.get("locations", []),
            "description", label,
        )
        xray["terms"] = _merge_from_prior(
            xray.get("terms", []),
            prior.get("terms", []),
            "definition", label,
        )
        # References carry over (mythological/literary context persists across series)
        xray["references"] = _merge_from_prior(
            xray.get("references", []),
            prior.get("references", []),
            "description", label,
        )

    return xray
