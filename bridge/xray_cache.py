"""
xray_cache.py — Per-book X-Ray cache in ~/.piread/cache/.

Structure:
  ~/.piread/cache/
    index.json          — quick-lookup table (title/author/hash/metadata)
    <md5_hash>.json     — full X-Ray data for one book

The index is what pi chat queries for ambient lookups.
The per-book files are what the bridge serves to the KOReader plugin.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR  = Path.home() / ".piread" / "cache"
INDEX_FILE = CACHE_DIR / "index.json"
_lock      = threading.Lock()


def _ensure() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── Full X-Ray data ────────────────────────────────────────────────────────────

def load(book_hash: str) -> dict | None:
    """Load full X-Ray record by hash. Returns None on miss."""
    path = CACHE_DIR / f"{book_hash}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Cache read error (%s): %s", path, e)
        return None


def save(book_hash: str, record: dict) -> None:
    """
    Persist a full X-Ray record and update the index.
    `record` should match the schema produced by xray_generator.build_record().
    """
    _ensure()
    path = CACHE_DIR / f"{book_hash}.json"
    with _lock:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        _update_index(book_hash, record)
    logger.info("Cache saved: %s (%s)", record.get("book", {}).get("title", "?"), book_hash)


def _update_index(book_hash: str, record: dict) -> None:
    index = _load_index()
    book  = record.get("book", {})
    xray  = record.get("xray", {})
    index["books"][book_hash] = {
        "hash":            book_hash,
        "title":           book.get("title", ""),
        "author":          book.get("author", ""),
        "series":          book.get("series"),
        "series_index":    book.get("series_index"),
        "calibre_id":      book.get("calibre_id"),
        "epub_path":       book.get("epub_path", ""),
        "generated_at":    record.get("generated_at", ""),
        "strategy":        record.get("strategy", ""),
        "character_count":  len(xray.get("characters", [])),
        "location_count":   len(xray.get("locations", [])),
        "term_count":       len(xray.get("terms", [])),
        "reference_count":  len(xray.get("references", [])),
        "timeline_count":   len(xray.get("timeline", [])),
        "last_reading_pct": record.get("last_reading_pct"),
    }
    index["updated"] = _now()
    INDEX_FILE.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update_reading_pct(book_hash: str, pct: float) -> None:
    """Update last-known reading position without regenerating."""
    _ensure()
    with _lock:
        index = _load_index()
        entry = index.get("books", {}).get(book_hash)
        if entry is not None:
            entry["last_reading_pct"] = round(pct, 1)
            index["updated"] = _now()
            INDEX_FILE.write_text(
                json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        # Also update the full record if it exists
        full = load(book_hash)
        if full:
            full["last_reading_pct"] = round(pct, 1)
            path = CACHE_DIR / f"{book_hash}.json"
            path.write_text(
                json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8"
            )


# ── Index queries (used by pi chat) ───────────────────────────────────────────

def load_index() -> dict:
    _ensure()
    return _load_index()


def find_by_title_author(title: str, author: str = "") -> dict | None:
    """
    Find a cached X-Ray by title (exact, case-insensitive).
    Returns the full record or None.
    """
    tl = title.lower().strip()
    al = author.lower().strip() if author else ""
    for book_hash, meta in _load_index().get("books", {}).items():
        if meta.get("title", "").lower().strip() == tl:
            if not al or al in meta.get("author", "").lower():
                return load(book_hash)
    return None


def list_cached() -> list[dict]:
    """Return all index entries — for pi chat browsing / 'what books do I have X-Ray for'."""
    return list(_load_index().get("books", {}).values())


def get_series(series_name: str) -> list[dict]:
    """Return full X-Ray records for every cached book in a series, sorted by index."""
    sn = series_name.lower().strip()
    results = []
    for book_hash, meta in _load_index().get("books", {}).items():
        if (meta.get("series") or "").lower().strip() == sn:
            rec = load(book_hash)
            if rec:
                results.append(rec)
    return sorted(results, key=lambda r: r.get("book", {}).get("series_index") or 0)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "updated": "", "books": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
