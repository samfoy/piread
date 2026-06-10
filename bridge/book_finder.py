"""
book_finder.py — Locate EPUBs in ~/CalibreLibrary by title and author.

Directory structure: Author Name/Title (calibre_id)/Title - Author Name.epub
Also reads metadata.opf for series info (more reliable than the EPUB-internal OPF).
"""

import logging
import os
import re
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

CALIBRE_LIB = Path(os.path.expanduser("~/CalibreLibrary"))

# Articles to strip when normalizing titles
_ARTICLES = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
# Subtitles after colon/dash/em-dash
_SUBTITLE  = re.compile(r"[\:—\u2014].*$")
# Non-word chars
_NONWORD   = re.compile(r"[^\w\s]")


def _norm_title(t: str) -> str:
    t = t.lower().strip()
    t = _SUBTITLE.sub("", t)
    t = _ARTICLES.sub("", t)
    t = _NONWORD.sub("", t)
    return " ".join(t.split())


def _norm_author(a: str) -> str:
    """Normalize author — 'Brown, Pierce' and 'Pierce Brown' both → 'brown pierce'."""
    a = a.lower().strip()
    if "," in a:
        parts = [p.strip() for p in a.split(",", 1)]
        a = f"{parts[1]} {parts[0]}"
    a = _NONWORD.sub("", a)
    return " ".join(a.split())


def _title_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _author_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Last-name match is a strong signal
    la = a.split()[-1] if a.split() else ""
    lb = b.split()[-1] if b.split() else ""
    if la and la == lb:
        return 0.9
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _read_calibre_metadata(book_dir: Path) -> dict:
    """
    Read series/index from Calibre's metadata.opf (more reliable than EPUB-internal).
    Returns {} if not found.
    """
    opf = book_dir / "metadata.opf"
    if not opf.exists():
        return {}
    try:
        tree = ET.parse(opf)
        ns = {"opf": "http://www.idpf.org/2007/opf",
              "dc":  "http://purl.org/dc/elements/1.1/"}
        series = series_index = None
        for meta in tree.findall(".//opf:meta", ns):
            name    = meta.get("name", "")
            content = meta.get("content", "")
            if name == "calibre:series":
                series = content
            elif name == "calibre:series_index":
                try:
                    series_index = int(float(content))
                except (ValueError, TypeError):
                    pass
        # Also grab title and author from dc: elements in case we need them
        def dc(tag):
            el = tree.find(f".//dc:{tag}", ns)
            return el.text.strip() if el is not None and el.text else ""
        return {
            "series":       series,
            "series_index": series_index,
            "title":        dc("title"),
            "author":       dc("creator"),
        }
    except Exception as e:
        logger.debug("metadata.opf parse error for %s: %s", book_dir, e)
        return {}


# ── Public API ─────────────────────────────────────────────────────────────────

def find_epub(title: str, author: str = "",
              calibre_lib: Path = CALIBRE_LIB) -> dict | None:
    """
    Find the best-matching book in CalibreLibrary.

    Returns a dict with:
      epub_path, calibre_id, title, author, series, series_index
    or None if no match with score ≥ 0.60.
    """
    norm_t = _norm_title(title)
    norm_a = _norm_author(author) if author else ""

    best: tuple[float, dict] | None = None

    for author_dir in calibre_lib.iterdir():
        if not author_dir.is_dir() or author_dir.name.startswith("."):
            continue

        dir_author_score = _author_score(norm_a, _norm_author(author_dir.name))
        # Skip author dirs that can't possibly match (saves time on big libraries)
        if norm_a and dir_author_score < 0.4:
            continue

        for book_dir in author_dir.iterdir():
            if not book_dir.is_dir():
                continue

            # Parse "Title (id)" format
            m = re.match(r"^(.*?)\s*\((\d+)\)$", book_dir.name)
            if not m:
                continue

            dir_title = _norm_title(m.group(1))
            calibre_id = int(m.group(2))

            ts = _title_score(norm_t, dir_title)
            # Author weight lower when no author provided
            author_weight = 0.35 if norm_a else 0.0
            title_weight  = 1.0 - author_weight
            score = ts * title_weight + dir_author_score * author_weight

            if score < 0.55:
                continue

            epub_files = list(book_dir.glob("*.epub"))
            if not epub_files:
                continue

            meta = _read_calibre_metadata(book_dir)

            candidate = {
                "epub_path":    str(epub_files[0]),
                "calibre_id":   calibre_id,
                "title":        meta.get("title") or m.group(1).strip(),
                "author":       meta.get("author") or author_dir.name,
                "series":       meta.get("series"),
                "series_index": meta.get("series_index"),
                "score":        round(score, 3),
            }

            if best is None or score > best[0]:
                best = (score, candidate)

    if best and best[0] >= 0.60:
        logger.info(
            "book_finder: matched '%s' by '%s' (score=%.2f, id=%d)",
            best[1]["title"], best[1]["author"], best[0], best[1]["calibre_id"]
        )
        return best[1]

    logger.info("book_finder: no match for '%s' / '%s'", title, author)
    return None


def get_series_books(series_name: str,
                     calibre_lib: Path = CALIBRE_LIB) -> list[dict]:
    """
    Return all books in a named series, sorted by series_index.
    Used for pre-generating full series X-Ray.
    """
    norm = series_name.lower().strip()
    results = []

    for author_dir in calibre_lib.iterdir():
        if not author_dir.is_dir():
            continue
        for book_dir in author_dir.iterdir():
            if not book_dir.is_dir():
                continue
            meta = _read_calibre_metadata(book_dir)
            if not meta.get("series"):
                continue
            if meta["series"].lower().strip() != norm:
                continue
            epub_files = list(book_dir.glob("*.epub"))
            if not epub_files:
                continue
            m = re.match(r"^(.*?)\s*\((\d+)\)$", book_dir.name)
            results.append({
                "epub_path":    str(epub_files[0]),
                "calibre_id":   int(m.group(2)) if m else 0,
                "title":        meta.get("title", book_dir.name),
                "author":       meta.get("author", author_dir.name),
                "series":       meta["series"],
                "series_index": meta.get("series_index", 0),
            })

    return sorted(results, key=lambda b: (b.get("series_index") or 0))


def list_all(calibre_lib: Path = CALIBRE_LIB) -> list[dict]:
    """Return every book in the library (for index building / pi chat queries)."""
    results = []
    for author_dir in calibre_lib.iterdir():
        if not author_dir.is_dir() or author_dir.name.startswith("."):
            continue
        for book_dir in author_dir.iterdir():
            if not book_dir.is_dir():
                continue
            m = re.match(r"^(.*?)\s*\((\d+)\)$", book_dir.name)
            if not m:
                continue
            epub_files = list(book_dir.glob("*.epub"))
            if not epub_files:
                continue
            meta = _read_calibre_metadata(book_dir)
            results.append({
                "epub_path":    str(epub_files[0]),
                "calibre_id":   int(m.group(2)),
                "title":        meta.get("title") or m.group(1).strip(),
                "author":       meta.get("author") or author_dir.name,
                "series":       meta.get("series"),
                "series_index": meta.get("series_index"),
            })
    return sorted(results, key=lambda b: (b["author"].lower(), b["title"].lower()))
