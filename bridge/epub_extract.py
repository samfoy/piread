"""
epub_extract.py — Extract text and structure from EPUB files.

Uses only stdlib (zipfile, xml.etree, html.parser) — no ebook-convert needed.
Handles EPUB 2 (NCX toc) and EPUB 3 (nav.xhtml toc).
"""

import hashlib
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


# ── Text extraction ────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Strip HTML tags, preserve paragraph structure."""

    BLOCK_TAGS = frozenset(
        "p div h1 h2 h3 h4 h5 h6 li tr br blockquote section article".split()
    )
    SKIP_TAGS = frozenset("script style head".split())

    def __init__(self):
        super().__init__()
        self._buf: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip = True
        elif tag in self.BLOCK_TAGS:
            if self._buf and self._buf[-1] != "\n":
                self._buf.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip = False
        elif tag in self.BLOCK_TAGS:
            if self._buf and self._buf[-1] != "\n":
                self._buf.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def result(self) -> str:
        text = "".join(self._buf)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.result()


# ── EPUB structure parsing ─────────────────────────────────────────────────────

def _find_opf_path(z: zipfile.ZipFile) -> str:
    """Locate the OPF file via META-INF/container.xml."""
    try:
        tree = ET.parse(z.open("META-INF/container.xml"))
        ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
        el = tree.find(".//c:rootfile", ns)
        if el is not None:
            return el.get("full-path", "")
    except Exception:
        pass
    # Fallback: first .opf in zip
    for name in z.namelist():
        if name.endswith(".opf"):
            return name
    raise ValueError("No OPF file found in EPUB")


def _parse_opf(z: zipfile.ZipFile, opf_path: str) -> tuple[list[str], dict, dict]:
    """
    Parse the OPF file.
    Returns:
      spine_paths  — list of HTML file paths in reading order
      metadata     — {title, author, series, series_index}
      toc_ncx_path — path to NCX file (or empty string)
    """
    opf_dir = os.path.dirname(opf_path)
    tree = ET.parse(z.open(opf_path))
    ns_opf = "http://www.idpf.org/2007/opf"
    ns_dc  = "http://purl.org/dc/elements/1.1/"
    ns = {"opf": ns_opf, "dc": ns_dc}

    # ── Manifest: id → (full_path, media_type) ────────────────────────────────
    manifest: dict[str, tuple[str, str]] = {}
    ncx_path = ""
    for item in tree.findall(".//opf:item", ns):
        item_id   = item.get("id", "")
        href      = item.get("href", "")
        mtype     = item.get("media-type", "")
        # Resolve path relative to OPF directory
        full = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
        # Normalize (collapse ../ etc)
        full = os.path.normpath(full).replace("\\", "/").lstrip("/")
        manifest[item_id] = (full, mtype)
        if mtype == "application/x-dtbncx+xml":
            ncx_path = full

    # ── Spine ─────────────────────────────────────────────────────────────────
    spine_paths: list[str] = []
    for itemref in tree.findall(".//opf:itemref", ns):
        idref = itemref.get("idref", "")
        if idref in manifest:
            full_path, mtype = manifest[idref]
            if "html" in mtype or full_path.endswith((".html", ".htm", ".xhtml")):
                spine_paths.append(full_path)

    if not spine_paths:
        # Fallback: all HTML files in manifest order
        spine_paths = [
            p for p, mt in manifest.values()
            if "html" in mt or p.endswith((".html", ".htm", ".xhtml"))
        ]

    # ── Metadata ──────────────────────────────────────────────────────────────
    def dc(tag):
        el = tree.find(f".//dc:{tag}", ns)
        return el.text.strip() if el is not None and el.text else ""

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

    metadata = {
        "title":        dc("title"),
        "author":       dc("creator"),
        "series":       series,
        "series_index": series_index,
    }

    return spine_paths, metadata, ncx_path


def _parse_toc(z: zipfile.ZipFile, ncx_path: str) -> dict[str, str]:
    """
    Return {filename_basename: chapter_title} from NCX or nav.xhtml.
    We key by basename so spine paths can be matched regardless of directory.
    """
    toc: dict[str, str] = {}

    # Try NCX (EPUB 2)
    if ncx_path:
        try:
            content = z.read(ncx_path).decode("utf-8", errors="replace")
            ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
            tree = ET.fromstring(content.encode())
            for np in tree.findall(".//ncx:navPoint", ns):
                label_el = np.find("ncx:navLabel/ncx:text", ns)
                src_el   = np.find("ncx:content", ns)
                if label_el is not None and src_el is not None:
                    title = (label_el.text or "").strip()
                    src   = src_el.get("src", "").split("#")[0]
                    basename = os.path.basename(src)
                    if title and basename:
                        toc[basename] = title
            if toc:
                return toc
        except Exception as e:
            logger.debug("NCX parse error: %s", e)

    # Try nav.xhtml (EPUB 3)
    for name in z.namelist():
        if name.endswith(("nav.xhtml", "nav.html", "toc.xhtml")):
            try:
                content = z.read(name).decode("utf-8", errors="replace")
                for m in re.finditer(
                    r'href=["\']([^"\'#]+)(?:#[^"\']*)?["\'][^>]*>\s*([^<]{1,120})',
                    content, re.IGNORECASE
                ):
                    basename = os.path.basename(m.group(1))
                    title    = re.sub(r"\s+", " ", m.group(2)).strip()
                    if title and basename:
                        toc[basename] = title
                if toc:
                    return toc
            except Exception as e:
                logger.debug("nav.xhtml parse error: %s", e)

    return toc


# ── Public API ─────────────────────────────────────────────────────────────────

@dataclass
class Chapter:
    title: str
    text: str
    position_pct: float        # 0–100, start of this chapter in the full book


@dataclass
class EpubContent:
    full_text: str
    chapters: list[Chapter]
    title: str
    author: str
    series: str | None
    series_index: int | None
    file_hash: str
    total_chars: int
    epub_path: str


# Chapter titles we want to skip (front/back matter)
_SKIP_TITLES = re.compile(
    r"^(cover|title page|copyright|dedication|table of contents|contents|"
    r"map|epigraph|about the author|acknowledgments?|also by|index|"
    r"back cover|further reading|notes?|bibliography|glossary)$",
    re.IGNORECASE,
)

# Looks like a real chapter
_CHAPTER_TITLES = re.compile(
    r"(chapter|part|prologue|epilogue|section|book \d|act \d)", re.IGNORECASE
)


def extract_epub(epub_path: str) -> EpubContent:
    """
    Extract full text and chapter structure from an EPUB file.
    Returns EpubContent with chapters ordered by reading position.
    """
    epub_path = str(epub_path)

    # Hash the file for caching
    with open(epub_path, "rb") as f:
        file_hash = hashlib.md5(f.read()).hexdigest()

    with zipfile.ZipFile(epub_path) as z:
        opf_path = _find_opf_path(z)
        spine_paths, metadata, ncx_path = _parse_opf(z, opf_path)
        toc = _parse_toc(z, ncx_path)

        # Extract text per spine item
        items: list[tuple[str, str, str]] = []  # (spine_path, chapter_title, text)
        for spine_path in spine_paths:
            try:
                html = z.read(spine_path).decode("utf-8", errors="replace")
            except KeyError:
                logger.debug("Spine item not found: %s", spine_path)
                continue

            text = _html_to_text(html)
            if not text.strip():
                continue

            basename      = os.path.basename(spine_path)
            chapter_title = toc.get(basename, "")
            items.append((spine_path, chapter_title, text))

    if not items:
        raise ValueError(f"No text could be extracted from {epub_path}")

    # ── Build chapter list ─────────────────────────────────────────────────────
    total_chars = sum(len(text) for _, _, text in items)
    char_offset = 0
    chapters:   list[Chapter] = []
    full_parts: list[str]     = []

    for spine_path, chapter_title, text in items:
        position_pct = (char_offset / total_chars * 100) if total_chars else 0

        # Only create a Chapter record for items that have a real title
        # (skip front/back matter, but still include their text in full_text)
        if chapter_title and not _SKIP_TITLES.match(chapter_title.strip()):
            chapters.append(Chapter(
                title=chapter_title,
                text=text,
                position_pct=round(position_pct, 2),
            ))
        elif not chapter_title:
            # Untitled items: look for a chapter heading in the text itself
            heading_match = re.search(
                r"^(Chapter \d+|Part \d+|CHAPTER \d+|Prologue|Epilogue|[IVX]+\.?\s+\w)",
                text, re.MULTILINE
            )
            if heading_match:
                chapters.append(Chapter(
                    title=heading_match.group(0).strip(),
                    text=text,
                    position_pct=round(position_pct, 2),
                ))

        full_parts.append(text)
        char_offset += len(text)

    full_text = "\n\n".join(full_parts)

    # If we got very few chapters (e.g. single-file EPUB), split by heading
    if len(chapters) <= 2 and full_text:
        logger.info("Few chapters detected (%d), splitting by headings", len(chapters))
        chapters = _split_by_headings(full_text, total_chars)

    logger.info(
        "Extracted '%s' — %d chars, %d chapters",
        metadata.get("title", "?"), total_chars, len(chapters)
    )

    return EpubContent(
        full_text=full_text,
        chapters=chapters,
        title=metadata.get("title", ""),
        author=metadata.get("author", ""),
        series=metadata.get("series"),
        series_index=metadata.get("series_index"),
        file_hash=file_hash,
        total_chars=total_chars,
        epub_path=epub_path,
    )


def _split_by_headings(full_text: str, total_chars: int) -> list[Chapter]:
    """Fallback: split full text at chapter headings."""
    pattern = re.compile(
        r"\n((?:Chapter|CHAPTER|Part|PART|Prologue|Epilogue|Book \d+|Act \d+)[^\n]{0,60})\n",
        re.IGNORECASE
    )
    parts = pattern.split(full_text)
    chapters = []
    char_offset = 0

    i = 0
    while i < len(parts):
        if i + 1 < len(parts) and pattern.match("\n" + parts[i] + "\n"):
            # parts[i] is a heading, parts[i+1] is content
            title   = parts[i].strip()
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            pct     = (char_offset / total_chars * 100) if total_chars else 0
            chapters.append(Chapter(title=title, text=content, position_pct=round(pct, 2)))
            char_offset += len(content)
            i += 2
        else:
            char_offset += len(parts[i])
            i += 1

    if not chapters:
        chapters = [Chapter(title="Full Text", text=full_text, position_pct=0.0)]

    return chapters
