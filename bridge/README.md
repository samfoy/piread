# piread-bridge

Lightweight HTTP bridge between KOReader's `piread` plugin and Claude via AWS Bedrock.
Runs on your Mac, listens for queries from KOReader over local WiFi.

## Architecture

```
Boox Palma (KOReader)
  └── piread.koplugin
        └── POST /ask  ──── local WiFi ────►  Mac :7731
                                               └── piread-bridge/server.py
                                                     └── AWS Bedrock → Claude
```

## Install

```bash
cd ~/Projects/piread-bridge
chmod +x install.sh
./install.sh
```

This installs a LaunchAgent (`com.sam.piread-bridge`) that starts automatically at login.

**Prerequisites:** `boto3` — `pip3 install boto3` if missing.

## Modes

| Mode | What it does |
|------|-------------|
| `whois` | Identify a character or term in the book's context |
| `explain` | Explain the passage (jargon, references, literary devices) |
| `summarize` | Describe the story context at this point in the narrative |
| `translate` | Translate selected text to English |

## Config

All config via environment variables in the plist (edit `com.sam.piread-bridge.plist`):

| Var | Default | Description |
|-----|---------|-------------|
| `PIREAD_PORT` | `7731` | TCP port to listen on |
| `PIREAD_AWS_PROFILE` | `openclaw-bedrock` | AWS credentials profile |
| `PIREAD_AWS_REGION` | `us-west-2` | Bedrock region |
| `PIREAD_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Model to use |
| `PIREAD_TOKEN` | *(empty)* | Optional shared secret for auth |
| `PIREAD_MAX_TOKENS` | `600` | Max response tokens |

After editing the plist, reload with:
```bash
launchctl unload ~/Library/LaunchAgents/com.sam.piread-bridge.plist
launchctl load   ~/Library/LaunchAgents/com.sam.piread-bridge.plist
```

## Logs

```bash
tail -f ~/Library/Logs/piread-bridge.log
```

## Quick test

```bash
curl -s http://localhost:7731/ping  # → pong

curl -s -X POST http://localhost:7731/ask \
  -H "Content-Type: application/json" \
  -d '{"text":"Darrow","context":"Darrow stood before the Senate.","book_title":"Red Rising","book_author":"Pierce Brown","mode":"whois"}' | python3 -m json.tool
```

## KOReader plugin

The plugin lives at `~/Projects/piread.koplugin/`. Copy or symlink it into KOReader's
external plugins directory on your device:

- **Boox Palma (Android KOReader):** `/sdcard/koreader/plugins/piread.koplugin/`
- **Kindle PW5 (KOReader):** `/mnt/us/koreader/plugins/piread.koplugin/`

On first use, go to **☰ → More tools → Pi reading assistant** to set the bridge host
to your Mac's IP or mDNS name (default: `macbook.local`), then tap **Test connection**.
