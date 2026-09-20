# Trading Wolf VIP Subscription Bot

Telegram bot for Trading Wolf's VIP signals funnel, cloned from the TradingWithNoahBot base and rebranded.

## What it does
- Welcomes users, answers FAQ-style questions (Hindi/Hinglish + English) about VIP signals, trading, deposits, etc.
- Runs an automated drip sequence of promo photos/videos with call-to-action buttons.
- Tracks verified traders and reminder state in Postgres.
- Includes `collector.py` — a small helper to grab fresh Telegram file IDs for this bot (photos/videos/video notes/documents can't be reused across different bots).

## Setup on Railway
1. New Project → Deploy from GitHub repo → this repo.
2. Add a PostgreSQL database in the same project (Railway sets `DATABASE_URL` automatically).
3. Set these variables on the bot service:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token from @BotFather |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `OWNER_ID` | Your numeric Telegram user ID |

4. Deploy. Check logs for confirmation the bot started successfully.

## Re-collecting media file IDs
Telegram file IDs are bot-specific — reused IDs from another bot will fail. To collect fresh ones for this bot:
1. Temporarily set the start command to `python collector.py` (needs `BOT_TOKEN` and optionally `OWNER_ID` set).
2. Send each photo/video/video note to the bot in a private chat — it replies with the file ID.
3. Swap each ID into `bot.py`, then switch the start command back to `python bot.py`.

## Known items still needing review
- One promotional caption (`REM5_CAP`) is written around "a photo of my parents" — the original bot's real family photo. Needs either a genuine replacement photo or a rewritten caption before use.
- `REM4_CAP` references a VIP member's "Z900 bike" purchase as a testimonial — verify or replace with a real member story if desired.
