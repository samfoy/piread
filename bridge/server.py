#!/usr/bin/env python3
"""
piread-bridge — KOReader → Claude Bedrock bridge.

Routes:
  GET  /ping                  health check → "pong"
  GET  /index                 X-Ray cache index (for pi chat queries)
  GET  /xray/status/<job_id>  poll a background X-Ray generation job
  POST /ask                   conversational query (explain/translate/summarize)
  POST /xray/init             find book in Calibre, generate X-Ray, cache it
  POST /xray/progress         update reading position for a cached book

Config via environment variables (all optional):
  PIREAD_PORT         TCP port to listen on           (default: 7731)
  PIREAD_AWS_PROFILE  AWS credentials profile          (default: openclaw-bedrock)
  PIREAD_AWS_REGION   Bedrock region                   (default: us-west-2)
  PIREAD_MODEL_ID     Model for /ask queries           (default: us.anthropic.claude-sonnet-4-6)
  PIREAD_TOKEN        Shared secret (empty = no auth)  (default: "")
  PIREAD_MAX_TOKENS   Max tokens for /ask responses    (default: 600)
"""

import json
import logging
import os
import signal
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from book_finder import find_epub
from epub_extract import extract_epub
from xray_generator import generate, build_record
import xray_cache

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = int(os.environ.get("PIREAD_PORT", 7731))
PROFILE    = os.environ.get("PIREAD_AWS_PROFILE", "openclaw-bedrock")
REGION     = os.environ.get("PIREAD_AWS_REGION", "us-west-2")
MODEL_ID   = os.environ.get("PIREAD_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
TOKEN      = os.environ.get("PIREAD_TOKEN", "")
MAX_TOKENS = int(os.environ.get("PIREAD_MAX_TOKENS", 600))

# ── System prompts per mode ───────────────────────────────────────────────────

SYSTEM_PROMPTS: dict[str, str] = {
    "whois": (
        "You are a reading assistant embedded in KOReader. "
        "The user selected a name or term they want identified. "
        "Explain who or what it is within the context of the book shown. "
        "Be concise (2–4 sentences). "
        "Do NOT reveal future plot events. "
        "Plain text only — no markdown."
    ),
    "explain": (
        "You are a reading assistant embedded in KOReader. "
        "The user wants a passage explained. "
        "Clarify difficult vocabulary, literary devices, historical references, "
        "or technical terms as needed. "
        "2–5 sentences. Plain text only — no markdown."
    ),
    "summarize": (
        "You are a reading assistant embedded in KOReader. "
        "The user wants to know the story context at this point in the book. "
        "Based on the passage and book info provided, briefly describe what has "
        "happened in the story up to this moment — who the main characters are "
        "and what situation they are in. "
        "3–6 sentences. Do NOT spoil future events. Plain text only — no markdown."
    ),
    "translate": (
        "You are a reading assistant embedded in KOReader. "
        "Translate the selected text into natural, readable English. "
        "If the text is already in English, note that and offer a plain-language "
        "paraphrase of any difficult sections. "
        "Plain text only — no markdown."
    ),
}

DEFAULT_SYSTEM = (
    "You are a helpful reading assistant embedded in KOReader. "
    "Answer the user's question about the selected text concisely. "
    "Plain text only — no markdown. Keep responses under 250 words."
)

# ── Bedrock client ────────────────────────────────────────────────────────────

def _bedrock_client():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    return session.client("bedrock-runtime")


def ask_claude(text: str, context: str | None, book_title: str | None,
               book_author: str | None, mode: str) -> str:
    system = SYSTEM_PROMPTS.get(mode, DEFAULT_SYSTEM)

    # Build the user message
    parts: list[str] = []
    if book_title:
        line = f'Book: "{book_title}"'
        if book_author:
            line += f" by {book_author}"
        parts.append(line)
    if context:
        parts.append(f"Surrounding passage:\n{context}")
    parts.append(f"Selected text: {text}")

    user_message = "\n\n".join(parts)

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
    }

    client = _bedrock_client()
    resp = client.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    result = json.loads(resp["body"].read())
    return result["content"][0]["text"].strip()


# ── X-Ray generation job registry ─────────────────────────────────────────────
# job_id → {status, progress, record, error}
_xray_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_xray_job(job_id: str, title: str, author: str, reading_pct: float) -> None:
    """Background thread: find book, extract, generate, cache."""
    def update(status: str, **kw):
        with _jobs_lock:
            _xray_jobs[job_id].update({"status": status, **kw})

    try:
        update("finding", progress="Looking up book in Calibre")
        book_meta = find_epub(title, author)
        if not book_meta:
            # Fallback: generate from Claude's knowledge (no EPUB needed)
            logging.info("Book not in Calibre, using knowledge-only mode: %s", title)
            update("generating", progress=f"Generating X-Ray from knowledge (no EPUB): {title}")
            _run_knowledge_xray_job(job_id, title, author)
            return

        update("extracting", progress="Extracting EPUB text")
        content = extract_epub(book_meta["epub_path"])

        chars = content.total_chars
        if chars <= 560_000:
            strat = "single_shot"
        elif chars <= 1_600_000:
            strat = "two_pass"
        else:
            strat = "chunked"
        update("generating",
               progress=f"Generating X-Ray via {strat} ({chars:,} chars)")

        xray, strategy = generate(content)
        record = build_record(content, book_meta, xray, strategy)
        if reading_pct:
            record["last_reading_pct"] = reading_pct
        xray_cache.save(content.file_hash, record)
        update("ready", record=record, error=None)
        logging.info("X-Ray job %s complete: %s", job_id, title)

    except Exception as exc:
        logging.exception("X-Ray job %s failed", job_id)
        update("failed", error=str(exc))


def _run_knowledge_xray_job(job_id: str, title: str, author: str) -> None:
    """
    Background thread: generate X-Ray from Claude's training knowledge alone.
    Used when the EPUB is not in Calibre (e.g. audiobook listeners).
    """
    def update(status: str, **kw):
        with _jobs_lock:
            _xray_jobs[job_id].update({"status": status, **kw})

    try:
        update("generating", progress=f"Generating X-Ray from knowledge: {title}")

        # Build a knowledge-only prompt
        system = (
            "You are a literary analyst. Generate a structured X-Ray for the book "
            "from your training knowledge. Return ONLY valid JSON. "
            "Your entire response must be one JSON object starting with '{' and ending with '}'."
        )
        prompt = f"""Generate a complete X-Ray for \"{title}\" by {author} from your knowledge of the book.

Return JSON matching exactly this structure:
{{
  \"book_type\": \"fiction\",
  \"characters\": [
    {{\"name\": str, \"role\": str, \"description\": str, \"aliases\": [str], \"first_appearance_pct\": 0}}
  ],
  \"locations\": [{{\"name\": str, \"description\": str, \"importance\": str}}],
  \"terms\": [{{\"name\": str, \"definition\": str, \"aliases\": [str]}}],
  \"historical_figures\": [{{\"name\": str, \"biography\": str, \"context_in_book\": str}}],
  \"references\": [
    {{\"name\": str, \"type\": \"literary|historical|mythological|cultural\", \"description\": str, \"context_in_book\": str, \"first_appearance_pct\": 0}}
  ],
  \"timeline\": [{{\"chapter\": str, \"event\": str, \"position_pct\": 0}}],
  \"author_info\": {{\"name\": str, \"bio\": str, \"born\": str, \"died\": null}}
}}

Generate 15-25 characters, 10-15 locations, 15-25 terms, 10-20 references, 25-40 timeline events.
For position_pct use your best estimate of where in the book each entity/event appears (0-100).
"""

        import json as _json
        import boto3
        from botocore.config import Config as _BotocoreConfig
        _cfg = _BotocoreConfig(read_timeout=600, connect_timeout=30)
        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        client = session.client("bedrock-runtime", config=_cfg)
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16000,  # knowledge-only needs more room
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        resp = client.invoke_model(
            modelId=MODEL_ID,
            body=_json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
        raw = _json.loads(resp["body"].read())["content"][0]["text"].strip()

        # Parse JSON (with repair fallback)
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError:
            start = raw.find("{")
            if start >= 0:
                raw = raw[start:]
            # Balance braces
            depth, in_str, esc = 0, False, False
            end = -1
            for i, c in enumerate(raw):
                if esc: esc = False; continue
                if c == "\\" and in_str: esc = True; continue
                if c == '"': in_str = not in_str; continue
                if not in_str:
                    if c == "{": depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0: end = i; break
            data = _json.loads(raw[:end+1] if end >= 0 else raw)

        from xray_generator import build_record
        from xray_cache import save
        import hashlib
        from epub_extract import EpubContent
        from datetime import datetime, timezone

        # Build a minimal EpubContent stand-in
        book_hash = hashlib.md5(f"{title}|{author}|knowledge".encode()).hexdigest()
        record = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "strategy": "knowledge_only",
            "book": {
                "title": title, "author": author,
                "series": None, "series_index": None,
                "calibre_id": None, "epub_path": None,
                "epub_hash": book_hash,
                "total_chars": 0, "chapter_count": 0,
            },
            "xray": data,
            "mentions": {},
        }
        save(book_hash, record)
        update("ready", record=record, error=None)
        logging.info("Knowledge X-Ray complete: %s", title)

    except Exception as exc:
        logging.exception("Knowledge X-Ray job %s failed", job_id)
        update("failed", error=str(exc))


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # redirect to Python logging
        logging.info("HTTP %s", fmt % args)

    # ── GET /ping ──────────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path == "/ping":
            self._send(200, b"pong", "text/plain")
        elif self.path == "/index":
            # Pi chat uses this to browse the X-Ray cache
            index = xray_cache.load_index()
            self._send_json(200, index)
        elif self.path.startswith("/xray/status/"):
            job_id = self.path.split("/xray/status/", 1)[-1]
            with _jobs_lock:
                job = _xray_jobs.get(job_id)
            if not job:
                self.send_error(404, "Unknown job")
                return
            # Don't send the full record in the status poll — just metadata
            resp = {"status": job["status"],
                    "progress": job.get("progress", ""),
                    "error": job.get("error")}
            if job["status"] == "ready" and job.get("record"):
                resp["xray"] = job["record"]["xray"]
                resp["book"] = job["record"]["book"]
            self._send_json(200, resp)
        else:
            self.send_error(404)

    # ── POST dispatch ─────────────────────────────────────────────────────────
    def do_POST(self):
        if self.path == "/xray/init":
            self._handle_xray_init()
            return
        if self.path == "/xray/progress":
            self._handle_xray_progress()
            return
        if self.path != "/ask":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            req = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.send_error(400, f"Invalid JSON: {exc}")
            return

        # Token check (optional)
        if TOKEN and req.get("token") != TOKEN:
            self.send_error(403, "Forbidden")
            return

        text = (req.get("text") or "").strip()
        if not text:
            self.send_error(400, "Missing 'text'")
            return

        try:
            response_text = ask_claude(
                text=text,
                context=req.get("context"),
                book_title=req.get("book_title"),
                book_author=req.get("book_author"),
                mode=req.get("mode", "explain"),
            )
            payload = {"response": response_text, "error": None}
            self._send_json(200, payload)

        except (BotoCoreError, ClientError) as exc:
            logging.error("Bedrock error: %s", exc)
            self._send_json(500, {"response": None, "error": f"Bedrock: {exc}"})
        except Exception as exc:
            logging.exception("Unexpected error")
            self._send_json(500, {"response": None, "error": str(exc)})

    # ── helpers ────────────────────────────────────────────────────────────────
    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── /xray/init ────────────────────────────────────────────────────────────
    def _handle_xray_init(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return

        title  = (req.get("book_title") or "").strip()
        author = (req.get("book_author") or "").strip()
        reading_pct = float(req.get("reading_pct") or 0)

        if not title:
            self.send_error(400, "Missing book_title"); return

        # ── Check cache first ──────────────────────────────────────────────────
        cached = xray_cache.find_by_title_author(title, author)
        if cached:
            logging.info("X-Ray cache HIT: %s", title)
            if reading_pct:
                xray_cache.update_reading_pct(cached["book"]["epub_hash"], reading_pct)
            self._send_json(200, {"status": "ready", "cached": True,
                                   "xray": cached["xray"], "book": cached["book"]})
            return

        # ── Start background generation job ─────────────────────────────────
        job_id = str(uuid.uuid4())[:8]
        with _jobs_lock:
            _xray_jobs[job_id] = {"status": "pending", "progress": "Starting",
                                   "record": None, "error": None}
        t = threading.Thread(
            target=_run_xray_job,
            args=(job_id, title, author, reading_pct),
            daemon=True,
        )
        t.start()
        logging.info("X-Ray job %s started for '%s'", job_id, title)
        self._send_json(202, {"status": "generating", "job_id": job_id,
                               "poll_url": f"/xray/status/{job_id}"})

    # ── /xray/progress ────────────────────────────────────────────────────────
    def _handle_xray_progress(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON"); return
        book_hash   = req.get("book_hash", "")
        reading_pct = float(req.get("reading_pct") or 0)
        if book_hash and reading_pct:
            xray_cache.update_reading_pct(book_hash, reading_pct)
        self._send(200, b"ok", "text/plain")

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self._send(code, body, "application/json")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log_file = os.path.expanduser("~/Library/Logs/piread-bridge.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    logging.info("piread-bridge listening on :%d  model=%s  profile=%s", PORT, MODEL_ID, PROFILE)

    def _shutdown(sig, _frame):
        logging.info("Shutting down (signal %d)", sig)
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
