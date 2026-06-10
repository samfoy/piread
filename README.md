# piread

AI-powered reading assistant for KOReader, backed by your local Mac via [pi](https://github.com/earendil-works/pi).

Generates rich **X-Ray entity graphs** (characters, locations, references, timeline) from your full Calibre library. Opens a contextual **"Now Reading" dashboard** showing who's on this page, where you are, and what literary/historical references the author is drawing on — without highlighting anything. Ask Pi conversational questions about the text.

Everything runs on your Mac. No external API keys. No cloud. Uses Claude via AWS Bedrock (the same credentials pi uses).

---

## What it does

| Feature | How |
|---------|-----|
| **X-Ray** | Characters with aliases + first-appearance tracking, locations, terms, literary/historical/mythological references, plot timeline | From your full Calibre EPUB |
| **Now Reading dashboard** | Who's on this page/chapter, what places and references appear here | Offline scan of cached X-Ray |
| **Conversational queries** | Highlight text → "Who is this?", "Explain this passage", "Story so far", "Translate" | Live Bedrock call |
| **Spoiler-free mode** | Hide characters/events past your current reading position | Structural filtering |
| **Series context** | Characters from earlier books pre-loaded when you open a sequel | Cross-book X-Ray merge |
| **Ambient pi chat** | Ask pi "who is Sevro?" or "what happened in Red Rising?" outside KOReader | `~/.piread/cache/` index |

## Architecture

```
KOReader (Palma / Kindle / any device)
  └── piread.koplugin
        ├── On book open  → POST /xray/init  (returns in <100ms from cache)
        ├── Now Reading   → offline scan of local X-Ray cache
        ├── Highlight     → POST /ask        (explain / translate / who is this)
        └── Polls         → GET  /xray/status/<job_id>  (30s interval while generating)

piread-bridge (your Mac, port 7731)
  ├── Finds book in ~/CalibreLibrary (fuzzy title+author match)
  ├── Extracts full EPUB text (zipfile, no ebook-convert needed)
  ├── Calls Claude via AWS Bedrock (same profile as pi)
  └── Caches at ~/.piread/cache/<hash>.json

pi chat
  └── Reads ~/.piread/cache/index.json for ambient book queries
```

## Requirements

- **Mac**: Python 3.10+, `boto3`, AWS Bedrock access (us.anthropic.claude-sonnet-4-6)
- **Device**: KOReader (any platform)
- **Same WiFi network** (or Tailscale)
- Calibre library at `~/CalibreLibrary` with EPUBs

## Setup

### 1. Bridge (Mac)

```bash
cd bridge
pip3 install boto3   # if not already installed
./install.sh         # installs as a LaunchAgent (auto-starts at login)
```

Test it's running:
```bash
curl http://localhost:7731/ping   # → pong
```

### 2. Plugin (KOReader)

Install via the KOReader AppStore plugin, or manually:

1. Download `piread.koplugin.zip` from [Releases](../../releases/latest)
2. Unzip into your KOReader plugins directory:
   - Android: `/sdcard/koreader/plugins/`
   - Kindle: `/mnt/us/koreader/plugins/`
3. Restart KOReader

### 3. First use

1. **☰ → More tools → Pi reading assistant → Test connection**
2. Set Host to your Mac's IP if `macbook.local` doesn't resolve (common on Android)
3. Open any book in your Calibre library — X-Ray generates in the background (~5 min first time, instant after)
4. **☰ → More tools → Pi reading assistant → Now Reading** to open the dashboard

## X-Ray quality (Red Rising example)

Single-shot strategy (full book in one Bedrock call, ~5 min):

- **24 characters** — aliases, roles, first-appearance %, descriptions (Darrow/Reaper/Lazarus, Sevro/Goblin, Virginia/Mustang, Adrius/Jackal...)
- **14 locations** — Lykos, Institute Valley, Olympus...
- **25 terms** — Colors caste system, slingBlade, gravBoots, The Passage...
- **16 references** — Lazarus (biblical), Persephone (Eo's martyrdom), Lord of the Flies (Institute parallel), The Count of Monte Cristo, Plato's Noble Lie (Gold supremacy speech), Spartan Agoge (Passage comparison), Cicero quote...
- **43 timeline events** — full narrative arc with chapter names and positions

## Ambient pi chat

After X-Ray is generated, ask pi directly:

> "Who is Sevro in Red Rising?"  
> "What's happened in Red Rising up to 45%?"  
> "Give me context before I start Golden Son"  
> "What books in my library have X-Ray data?"

The `~/.pi/agent/skills/piread/SKILL.md` skill handles these queries from the cache — no Bedrock call needed.

## Bridge config

All via environment variables in `bridge/com.sam.piread-bridge.plist`:

| Variable | Default | Description |
|----------|---------|-------------|
| `PIREAD_AWS_PROFILE` | `openclaw-bedrock` | AWS credentials profile |
| `PIREAD_AWS_REGION` | `us-west-2` | Bedrock region |
| `PIREAD_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Model |
| `PIREAD_PORT` | `7731` | Listen port |
| `PIREAD_TOKEN` | *(empty)* | Optional shared secret |

## Logs

```bash
tail -f ~/Library/Logs/piread-bridge.log
```

## License

MIT
