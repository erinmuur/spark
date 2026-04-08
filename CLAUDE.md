# Spark

Short-form video inspiration board with AI-powered creative brief generation.

## What it does
- Ingests video links posted in a Slack channel (TikTok, Instagram, Twitter/X)
- Auto-classifies each video against a library of user-defined video frameworks
- Generates editable creative briefs ("campaigns") for Readwise products

## Stack
- Python + Flask
- SQLite + SQLAlchemy
- HTMX + Pico CSS
- Anthropic Claude API
- slack_bolt (Python, Socket Mode)
- yt-dlp

## Run
```bash
cd ~/spark && source venv/bin/activate && flask run --port 5003
```

## Slack bot (separate process)
```bash
cd ~/spark && source venv/bin/activate && python bot.py
```

## Port
Always use port 5003 (5001 = Mog, 5002 = Creator Graph).

## Notes
- Port 5000 is taken on this Mac (AirPlay) — never use it
- Ctrl+C doesn't work in terminal (bound to Raycast) — close window to stop
- No auth required
- Products: Readwise, Reader, Bookwise
