# Trading Wolf Bot

Telegram bot that runs a full registration → deposit → VIP funnel for Trading Wolf, with an automated onboarding sequence, a 50% deposit bonus, Quotex postback verification, and a repeating 4-part reminder drip.

## What it does

- `/start` kicks off a timed onboarding sequence (sticker, photos, face-cam videos, a media group) spread over several automatic stages, each a few minutes apart.
- User clicks **Register**, opens a Quotex account through the Wolf affiliate link, and sends back their Trader ID.
- The bot verifies that ID against Quotex postbacks (see below) and checks their deposit.
- Once the minimum deposit is met, the user unlocks the VIP signals group link.
- A **"Claim 50% Bonus"** button is available any time during the deposit-waiting stage — it sends the bonus photo + the `WOLF50` promo code.
- Every 10 hours after registering, the bot sends one of 4 repeating reminder messages (photo/video + caption) to nudge users who haven't deposited yet. The cycle repeats indefinitely: 1 → 2 → 3 → 4 → 1 → ...
- A built-in FAQ/keyword responder (`smart_reply`, called via `ask_gemini`) answers common Hindi/English questions about trading, VIP, deposits, etc. — this is a rule-based matcher, not a live AI API call, so it needs no API key and has no rate limits.
- The bot owner (`OWNER_ID`) can send any photo/video/video-note/sticker/document directly to the bot to get back its Telegram `file_id`, for wiring into the variables below.
- A daily summary (starts, registrations, deposits, verified traders) is sent to the owner at 12:00 AM IST.

## Setup on Railway

1. New Project → Deploy from GitHub repo → this repo.
2. Add a PostgreSQL database to the project (used to track verified traders, postback logs, and reminder scheduling).
3. Set the required variables below on the bot service, then deploy.

### Required variables

| Variable | What it's for |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token from @BotFather |
| `DATABASE_URL` | Postgres connection string |
| `ADMIN_KEY` | Secret for the `/addid` manual-verification endpoint |
| `POSTBACK_KEY` | Secret Quotex includes in its postback URL — must match what you configured in Quotex |
| `OWNER_ID` | Your Telegram numeric user ID (for file_id collection + daily reports) |

### Core config (has working defaults, override if needed)

`AFFILIATE`, `REGISTER_LINK`, `TG_CHANNEL`, `VIP_LINK`, `SUPPORT`, `SUPPORT_USER`, `MIN_DEPOSIT`, `DEPOSIT_VALID_DAYS`, `YOUTUBE`, `COURSE_LINK`

### Media (all optional — bot skips gracefully if unset)

Onboarding sequence: `SEQ1_STICKER`, `SEQ1_PHOTO`, `SEQ1_VIDEONOTE`, `MEDIA_GROUP_1`–`MEDIA_GROUP_10` (prefix a value with `v:` for a video item), `SEQ3_PHOTO`, `SEQ3_VIDEONOTE`, `SEQ4_VIDEO`, `REGISTERED_STEP_PHOTO`, `BONUS_PHOTO`, `VIDEO_TUTORIAL`

Reminders (repeat every 10 hours, cycle 1→2→3→4→1...): `REM1_PHOTO`, `REM1_VIDEONOTE`, `REM1_CAPTION`, `REM2_VIDEO`, `REM2_CAPTION`, `REM3_PHOTO`, `REM3_VIDEONOTE`, `REM3_CAPTION`, `REM4_VIDEO`, `REM4_CAPTION`

To get a `file_id`: send the photo/video/sticker/video-note directly to the bot as the owner — it replies with the ID to copy into the matching variable above.

## Postback (Quotex)

Configure this URL in Quotex's postback settings:

```
https://<your-railway-domain>/postback?uid={trader_id}&sumdep={sumdep}&key=<POSTBACK_KEY>
```

The bot verifies the `key` matches `POSTBACK_KEY`, then records the deposit against that Trader ID.

## Editing message text

Most onboarding/sequence text is in `bot.py` directly (search for the relevant `_seq*_text` or caption block). Reminder captions (`REM1_CAPTION`–`REM4_CAPTION`) and all media file_ids are environment variables, so they can be changed without touching code.
