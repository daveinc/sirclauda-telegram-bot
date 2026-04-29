# SirClauda — Telegram Bot for Claude Code

Control and chat with your local Claude Code sessions remotely via Telegram.
No Anthropic API key needed — uses the local `claude` CLI directly.

## Setup

See the full blueprint at `blueprint-telegram-claude-bot.md` for complete instructions.

### Quick start

1. Get a bot token from `@BotFather` on Telegram
2. Copy `.env.example` to `.env` and fill in your values
3. Install dependencies: `pip install -r requirements.txt`
4. Run: `python watchdog.py`

## Features

- Chat with Claude remotely via Telegram
- Address specific Claude Code project tabs with `#tagname`
- Sticky sessions — tag once, keep chatting without re-typing the tag
- Completion acknowledgements and 10-word summaries per session
- Subscribe/unsubscribe to per-tab completion events
- Watchdog with ETA-aware freeze detection and dead switch

## Commands

| Command | What it does |
|---|---|
| `/tab` | Show current active tab |
| `/tab #name` | Switch active tab |
| `/tabs` | List all sessions |
| `/subscriptions` | List subscribed tabs |
| `/clear [tab]` | Clear a session |
| `subscribe to #name events` | Subscribe to completion summaries |
| `unsubscribe from #name` | Unsubscribe |
