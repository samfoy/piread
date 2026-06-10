"""
xray_generator.py — Generate X-Ray entity graphs from EPUB content via Bedrock.

Three strategies based on book size:
  single_shot   < 560K chars  (~140K tokens) — one Claude call, full text
  two_pass      < 1.6M chars  (~400K tokens) — two halves, merge
  chunked       anything larger               — chapter groups ~120K tokens each

The generated X-Ray is spoiler-complete (all positions tagged 0–100%).
Filtering to the reader's current position happens at serve-time, not here.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import BotoCoreError, ClientError

from epub_extract import EpubContent, Chapter

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

PROFILE    = os.environ.get("PIREAD_AWS_PROFILE", "openclaw-bedrock")
REGION     = os.environ.get("PIREAD_AWS_REGION", "us-west-2")
MODEL_ID   = os.environ.get("PIREAD_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("PIREAD_XRAY_MAX_TOKENS", "16384"))

# Large context requests can take several minutes
_BEDROCK_CONFIG = BotocoreConfig(
    read_timeout=600,        # 10 min — large books take time
    connect_timeout=30,
    retries={"max_attempts": 2, "mode": "standard"},
)

SINGLE_SHOT_LIMIT = 760_000    # chars — single Bedrock call (~190K tokens, fits 200K ctx)
TWO_PASS_LIMIT    = 1_600_000  # chars — split into two halves
CHUNK_SIZE        = 480_000    # chars per chunk for large books


# ── Prompts ────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a literary analyst. Extract structured X-Ray data from book text. "
    "Return ONLY valid JSON — no markdown, no commentary. "
    "Start your response immediately with a JSON object."
)

_SCHEMA_COMMENT = """\
JSON schema (all fields required, use empty arrays not null):
{
  "book_type": "fiction" | "nonfiction",
  "characters": [
    {"name": str, "role": str (<40 chars), "description": str (<250 chars),
     "aliases": [str], "first_appearance_pct": int (0-100)}
  ],
  "locations": [
    {"name": str, "description": str (<120 chars), "importance": str (<60 chars)}
  ],
  "terms": [
    {"name": str, "definition": str (<150 chars), "aliases": [str]}
  ],
  "historical_figures": [
    {"name": str, "biography": str (<150 chars), "context_in_book": str (<80 chars)}
  ],
  "timeline": [
    {"chapter": str, "event": str (<120 chars), "position_pct": int (0-100)}
  ],
  "author_info": {"name": str, "bio": str (<200 chars), "born": str|null, "died": str|null}
}"""


def _full_prompt(title: str, author: str, book_text: str,
                 series_tag: str = "") -> str:
    header = f'Book: "{title}" by {author}'
    if series_tag:
        header += f" — {series_tag}"
    return (
        f"{header}\n\n"
        f"Extract the complete X-Ray from the full book text below.\n"
        f"For each character, set first_appearance_pct to where they first appear (0=start, 100=end).\n"
        f"For each timeline event, set position_pct to where it occurs.\n\n"
        f"{_SCHEMA_COMMENT}\n\n"
        f"<book_text>\n{book_text}\n</book_text>"
    )


def _chunk_prompt(title: str, author: str, section_text: str,
                  start_pct: float, end_pct: float,
                  known_names: list[str] | None = None,
                  series_tag: str = "") -> str:
    header = f'Book: "{title}" by {author}'
    if series_tag:
        header += f" — {series_tag}"
    known = ""
    if known_names:
        # Trim to avoid ballooning context
        sample = ", ".join(known_names[:60])
        known = (
            f"\nAlready found in earlier sections (avoid duplicates unless new info): {sample}\n"
        )
    return (
        f"{header}\n"
        f"Section: {start_pct:.0f}%–{end_pct:.0f}% of the book\n"
        f"{known}\n"
        f"Extract X-Ray for THIS SECTION. Use position_pct values within the full book "
        f"(this section starts at {start_pct:.0f}%).\n\n"
        f"{_SCHEMA_COMMENT}\n\n"
        f"<section_text>\n{section_text}\n</section_text>"
    )


# ── Bedrock client ─────────────────────────────────────────────────────────────

def _client():
    return boto3.Session(profile_name=PROFILE, region_name=REGION).client(
        "bedrock-runtime", config=_BEDROCK_CONFIG
    )


def _call(prompt: str) -> str:
    """Make a Bedrock call. Returns raw response text."""
    # Note: Bedrock does not support assistant-role prefill.
    # We rely on the system prompt + explicit JSON instruction instead.
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "system": _SYSTEM,
        "messages": [
            {"role": "user", "content": prompt},
        ],
    }
    resp   = _client().invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(resp["body"].read())
    return result["content"][0]["text"]


# ── JSON parsing + repair ──────────────────────────────────────────────────────

def _parse(raw: str) -> dict:
    raw = raw.strip()
    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Find first { and try to balance
    start = raw.find("{")
    if start == -1:
        raise ValueError("No JSON object in response")
    raw = raw[start:]

    depth, in_str, esc = 0, False, False
    end = -1
    for i, c in enumerate(raw):
        if esc:
            esc = False; continue
        if c == "\\" and in_str:
            esc = True; continue
        if c == '"':
            in_str = not in_str; continue
        if not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i; break

    fragment = raw[: end + 1] if end >= 0 else raw
    return json.loads(fragment)


# ── Data normalization and merge ───────────────────────────────────────────────

def _norm_list(data: dict, key: str) -> list[dict]:
    items = data.get(key) or []
    if not isinstance(items, list):
        return []
    return [
        {k.lower().replace(" ", "_"): v for k, v in item.items()}
        for item in items
        if isinstance(item, dict) and item.get("name")
    ]


def _norm_timeline(data: dict) -> list[dict]:
    """Timeline items use 'chapter'/'event' keys, not 'name' — handle separately."""
    items = data.get("timeline") or []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Accept chapter or chapter_title as the chapter key
        chapter = item.get("chapter") or item.get("chapter_title") or ""
        event   = item.get("event")   or item.get("summary") or item.get("description") or ""
        if not chapter or not event:
            continue
        result.append({
            "chapter":      str(chapter).strip(),
            "event":        str(event).strip(),
            "position_pct": int(item.get("position_pct") or item.get("pct") or 0),
        })
    return result


def _normalize(raw: dict) -> dict:
    return {
        "book_type":          raw.get("book_type", "fiction"),
        "characters":         _norm_list(raw, "characters"),
        "locations":          _norm_list(raw, "locations"),
        "terms":              _norm_list(raw, "terms"),
        "historical_figures": _norm_list(raw, "historical_figures"),
        "references":          _norm_list(raw, "references"),
        "timeline":           sorted(
            _norm_timeline(raw),
            key=lambda e: e.get("position_pct", 0)
        ),
        "author_info":        raw.get("author_info"),
    }


def _dedup(existing: list[dict], incoming: list[dict],
           desc_key: str = "description") -> list[dict]:
    """Merge incoming into existing, deduplicating by name (case-insensitive)."""
    index = {item["name"].lower(): i for i, item in enumerate(existing)}
    for item in incoming:
        name_lo = item.get("name", "").lower()
        if not name_lo:
            continue
        if name_lo in index:
            i = index[name_lo]
            # Keep earlier first_appearance_pct
            if item.get("first_appearance_pct", 100) < existing[i].get("first_appearance_pct", 100):
                existing[i]["first_appearance_pct"] = item["first_appearance_pct"]
            # Prefer longer description
            if len(item.get(desc_key, "")) > len(existing[i].get(desc_key, "")):
                existing[i][desc_key] = item[desc_key]
            # Merge aliases
            aa = set(existing[i].get("aliases", []))
            aa.update(item.get("aliases", []))
            existing[i]["aliases"] = sorted(aa)
        else:
            existing.append(item)
            index[name_lo] = len(existing) - 1
    return existing


def _merge(results: list[dict]) -> dict:
    merged: dict = {
        "book_type":          "fiction",
        "characters":         [],
        "locations":          [],
        "terms":              [],
        "historical_figures": [],
        "references":          [],
        "timeline":           [],
        "author_info":        None,
    }
    for r in results:
        if not r:
            continue
        merged["book_type"] = r.get("book_type", merged["book_type"])
        merged["characters"]         = _dedup(merged["characters"],         r.get("characters", []))
        merged["locations"]          = _dedup(merged["locations"],          r.get("locations", []),  "description")
        merged["terms"]              = _dedup(merged["terms"],              r.get("terms", []),      "definition")
        merged["historical_figures"] = _dedup(merged["historical_figures"], r.get("historical_figures", []), "biography")
        merged["references"]          = _dedup(merged["references"],          r.get("references", []),          "description")
        merged["timeline"].extend(r.get("timeline", []))
        if r.get("author_info") and not merged["author_info"]:
            merged["author_info"] = r["author_info"]

    merged["timeline"].sort(key=lambda e: e.get("position_pct", 0))
    merged["characters"].sort(key=lambda c: c.get("first_appearance_pct", 0))
    return merged


# ── Strategies ─────────────────────────────────────────────────────────────────

def _series_tag(content: EpubContent) -> str:
    if content.series:
        tag = content.series
        if content.series_index:
            tag += f", Book {content.series_index}"
        return tag
    return ""


def _annotated_text(chapters: list[Chapter],
                    start_pct: float = 0.0,
                    end_pct:   float = 100.0) -> tuple[str, int]:
    """
    Build text with embedded chapter markers Claude uses for timeline events:
        [CHAPTER: "Title" | position: 12%]
    Returns (text, char_count). Includes chapters whose position is in [start_pct, end_pct).
    """
    parts = []
    for ch in chapters:
        if ch.position_pct < start_pct:
            continue
        if end_pct < 100.0 and ch.position_pct >= end_pct:
            break
        marker = f'[CHAPTER: "{ch.title}" | position: {ch.position_pct:.0f}%]'
        parts.append(f"{marker}\n{ch.text}")
    text = "\n\n".join(parts)
    return text, len(text)


def _split_chapters_at(
    chapters: list[Chapter], split_pct: float = 50.0
) -> tuple[list[Chapter], list[Chapter]]:
    """Split chapters into two halves at the chapter boundary nearest to split_pct."""
    if not chapters:
        return [], []
    best_i = min(
        range(1, len(chapters)),
        key=lambda i: abs(chapters[i].position_pct - split_pct),
        default=len(chapters) // 2,
    )
    return chapters[:best_i], chapters[best_i:]


# ── Prompts ────────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a literary analyst. Extract structured X-Ray data from book text. "
    "Return ONLY valid JSON — no markdown, no commentary, no prefix text. "
    "Your entire response must be one JSON object starting with '{' and ending with '}'."
)

_SCHEMA = """\
Return a JSON object with these exact fields (use [] not null for empty arrays):
{
  \"book_type\": \"fiction\" or \"nonfiction\",
  \"characters\": [
    {
      \"name\": \"Full name as used in the book\",
      \"role\": \"Under 40 chars. e.g. 'Protagonist, Red Helldiver'\",
      \"description\": \"Under 250 chars. Who they are, their arc, key traits.\",
      \"aliases\": [\"other names or titles used for this character\"],
      \"first_appearance_pct\": 0
    }
  ],
  \"locations\": [
    {
      \"name\": \"Place name\",
      \"description\": \"Under 120 chars.\",
      \"importance\": \"Under 60 chars. Why it matters.\"
    }
  ],
  \"terms\": [
    {
      \"name\": \"The term, jargon, or concept\",
      \"definition\": \"Under 150 chars.\",
      \"aliases\": [\"related terms or alternate spellings\"]
    }
  ],
  \"historical_figures\": [
    {
      \"name\": \"Real person's name\",
      \"biography\": \"Under 150 chars. Real-world info.\",
      \"context_in_book\": \"Under 80 chars. How they're referenced.\"
    }
  ],
  \"references\": [
    {
      \"name\": \"Name of the real-world work, figure, place, or concept being invoked\",
      \"type\": \"literary, historical, mythological, cultural, scientific, biblical, or philosophical\",
      \"description\": \"Under 150 chars. What this is in the real world.\",
      \"context_in_book\": \"Under 100 chars. How the book invokes, parallels, or uses it.\",
      \"first_appearance_pct\": 0
    }
  ],
  \"timeline\": [
    {
      \"chapter\": \"Chapter title copied exactly from [CHAPTER: \\\"...\\\"] marker\",
      \"event\": \"Under 120 chars. One clear sentence: what happens and to whom.\",
      \"position_pct\": 0
    }
  ],
  \"author_info\": {
    \"name\": \"Author name\",
    \"bio\": \"Under 200 chars.\",
    \"born\": \"year or null\",
    \"died\": \"year or null\"
  }
}"""

_REFERENCE_RULES = """\
References: Extract 10-25 external references — real-world works, historical events, mythological figures, philosophical concepts, or cultural touchstones that the author explicitly invokes or structurally parallels. Only include references where the book clearly draws on them, not vague thematic similarities. For each, set first_appearance_pct to where it first appears."""

_TIMELINE_RULES = """\
Timeline rules:
- Extract 25–45 significant plot events in chronological order.
- For each event, use the nearest [CHAPTER: \"...\"] marker above it to set both
  \"chapter\" (copy the title exactly) and \"position_pct\" (copy the number).
- Every event must describe a concrete action, revelation, or turning point
  (not setting descriptions or character introductions — those go in characters/locations).
- Do NOT leave timeline as an empty array. If the text has a story, it has events."""


def _full_prompt(title: str, author: str, annotated_text: str,
                 series_tag: str = "") -> str:
    header = f'Book: "{title}" by {author}'
    if series_tag:
        header += f" ({series_tag})"
    return (
        f"{header}\n\n"
        f"The book text below contains [CHAPTER: \"Title\" | position: N%] markers "
        f"before each chapter. Use them to assign chapter names and position values.\n\n"
        f"{_SCHEMA}\n\n"
        f"{_REFERENCE_RULES}\n\n"
        f"{_TIMELINE_RULES}\n\n"
        f"<book_text>\n{annotated_text}\n</book_text>"
    )


def _chunk_prompt(title: str, author: str, annotated_section: str,
                  start_pct: float, end_pct: float,
                  known_names: list[str] | None = None,
                  series_tag: str = "") -> str:
    header = f'Book: "{title}" by {author}'
    if series_tag:
        header += f" ({series_tag})"
    known = ""
    if known_names:
        sample = ", ".join(known_names[:60])
        known = f"Previously found characters (skip duplicates; add only if new info present): {sample}\n\n"
    return (
        f"{header}\n"
        f"This section covers {start_pct:.0f}%–{end_pct:.0f}% of the book.\n\n"
        f"{known}"
        f"The section contains [CHAPTER: \"Title\" | position: N%] markers. "
        f"Use them for chapter names and position_pct values.\n\n"
        f"{_SCHEMA}\n\n"
        f"{_REFERENCE_RULES}\n\n"
        f"{_TIMELINE_RULES}\n\n"
        f"<section_text>\n{annotated_section}\n</section_text>"
    )


# ── Strategies ────────────────────────────────────────────────────────────────────

def _single_shot(content: EpubContent) -> dict:
    logger.info("Strategy: single_shot (%d chars, %d chapters)",
                content.total_chars, len(content.chapters))
    text, n = _annotated_text(content.chapters)
    logger.info("  annotated text: %d chars", n)
    prompt = _full_prompt(content.title, content.author, text, _series_tag(content))
    raw  = _call(prompt)
    data = _parse(raw)
    return _normalize(data)


def _two_pass(content: EpubContent) -> dict:
    logger.info("Strategy: two_pass (%d chars, %d chapters)",
                content.total_chars, len(content.chapters))
    first_half, second_half = _split_chapters_at(content.chapters, 50.0)
    split_pct = second_half[0].position_pct if second_half else 50.0
    logger.info("  split at %.0f%% (%d + %d chapters)",
                split_pct, len(first_half), len(second_half))

    results:     list[dict] = []
    known_names: list[str]  = []
    tag = _series_tag(content)

    for i, half in enumerate([first_half, second_half]):
        if not half:
            continue
        s = half[0].position_pct
        e = second_half[0].position_pct if i == 0 and second_half else 100.0
        text, n = _annotated_text(half)
        logger.info("  pass %d: %.0f%%–%.0f%% (%d chars)", i + 1, s, e, n)
        prompt = _chunk_prompt(content.title, content.author, text, s, e, known_names, tag)
        try:
            xray = _normalize(_parse(_call(prompt)))
            results.append(xray)
            known_names.extend(c["name"] for c in xray["characters"])
        except Exception as exc:
            logger.warning("  pass %d failed: %s", i + 1, exc)

    return _merge(results)


def _chunked(content: EpubContent) -> dict:
    logger.info("Strategy: chunked (%d chars, %d chapters)",
                content.total_chars, len(content.chapters))

    # Group chapters into ~CHUNK_SIZE char blobs
    groups: list[tuple[list[Chapter], float, float]] = []
    buf: list[Chapter] = []
    buf_size = 0

    for ch in content.chapters:
        ch_size = len(ch.title) + len(ch.text) + 40  # ~40 chars for marker
        if buf and buf_size + ch_size > CHUNK_SIZE:
            next_start = ch.position_pct
            groups.append((list(buf), buf[0].position_pct, next_start))
            buf, buf_size = [ch], ch_size
        else:
            buf.append(ch)
            buf_size += ch_size

    if buf:
        groups.append((buf, buf[0].position_pct, 100.0))

    logger.info("  %d chunk groups", len(groups))

    results:     list[dict] = []
    known_names: list[str]  = []
    tag = _series_tag(content)

    for i, (chapters, s, e) in enumerate(groups):
        text, n = _annotated_text(chapters)
        logger.info("  chunk %d/%d (%.0f%%–%.0f%%, %d chars)",
                    i + 1, len(groups), s, e, n)
        prompt = _chunk_prompt(content.title, content.author, text, s, e, known_names, tag)
        try:
            xray = _normalize(_parse(_call(prompt)))
            results.append(xray)
            known_names.extend(c["name"] for c in xray["characters"])
        except Exception as exc:
            logger.warning("  chunk %d failed: %s", i + 1, exc)

    return _merge(results)


# ── Public API ─────────────────────────────────────────────────────────────────

def generate(content: EpubContent) -> dict:
    """
    Generate a complete X-Ray for a book.
    Returns a normalized xray dict ready to embed in a cache record.
    Raises on unrecoverable failure.
    """
    if content.total_chars <= SINGLE_SHOT_LIMIT:
        xray = _single_shot(content)
        strategy = "single_shot"
    elif content.total_chars <= TWO_PASS_LIMIT:
        xray = _two_pass(content)
        strategy = "two_pass"
    else:
        xray = _chunked(content)
        strategy = "chunked"

    logger.info(
        "Generated X-Ray: %d characters | %d locations | %d terms | %d hist_figs | %d timeline_events",
        len(xray.get("characters", [])),
        len(xray.get("locations", [])),
        len(xray.get("terms", [])),
        len(xray.get("historical_figures", [])),
        len(xray.get("timeline", [])),
    )
    return xray, strategy


def build_record(content: EpubContent, book_meta: dict, xray: dict,
                 strategy: str) -> dict:
    """
    Assemble a complete cache record from extracted content and generated X-Ray.
    This is what gets written to ~/.piread/cache/<hash>.json
    """
    return {
        "version":      1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategy":     strategy,
        "book": {
            "title":        content.title,
            "author":       content.author,
            "series":       content.series,
            "series_index": content.series_index,
            "calibre_id":   book_meta.get("calibre_id"),
            "epub_path":    content.epub_path,
            "epub_hash":    content.file_hash,
            "total_chars":  content.total_chars,
            "chapter_count": len(content.chapters),
        },
        "xray": xray,
    }
