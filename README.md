# Trading Wolf — Educational Bot

Telegram bot that teaches trading concepts (candlesticks, chart patterns, risk management, psychology, etc.) through an inline-button menu, and points users to your community channel.

## What it does
- `/start` shows a menu of educational topics as buttons.
- Tapping a topic shows a write-up, and a video link where one is set.
- Every screen includes a "Join Our Community" button linking to your channel.
- No database, no user tracking, no deposit/VIP funnel — this bot only needs your bot token.

## Setup on Railway
1. New Project → Deploy from GitHub repo → this repo.
2. Set one variable on the service:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token from @BotFather |

3. Deploy. Check logs for confirmation the bot started successfully ("Trading Wolf (educational) bot started — polling.").

## Editing topics
All topic content lives in the `TOPICS` dict near the top of `bot.py` — edit the `text` and `video_url` fields directly and redeploy.
