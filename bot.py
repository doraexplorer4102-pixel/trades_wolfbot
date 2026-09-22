import os
import asyncio
import ssl
import logging
import threading
import time

# ── LOGGING (keep Railway log volume sane) ───────────────────────────────────
# Without this, httpx logs a line for EVERY Telegram API call and telegram/aiohttp
# add their own — easily hundreds per second under load. We keep our own messages
# but turn the chatty libraries down to WARNING so only real problems show up.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for _noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "aiohttp.access", "apscheduler"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo
)
from telegram.ext import (
    AIORateLimiter,
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
from telegram.error import Forbidden
import pg8000.native

def _require_env(name):
    """Read a required secret from the environment, with a clear error if missing."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in Railway → your service → Variables tab."
        )
    return val

# ── SECRETS (loaded from environment, never hardcoded) ───────────────────────
TOKEN         = _require_env("BOT_TOKEN")
DATABASE_URL  = _require_env("DATABASE_URL")
ADMIN_KEY     = _require_env("ADMIN_KEY")
POSTBACK_KEY  = _require_env("POSTBACK_KEY")

VIP_LINK      = os.environ.get("VIP_LINK", "https://t.me/+3MN-Y0Q5CpYzMjZl")
AFFILIATE     = os.environ.get("AFFILIATE", "https://broker-qx.pro/sign-up/?lid=2362141")
SUPPORT       = os.environ.get("SUPPORT", "https://t.me/WOLF_BINARYSIGNALS")
MIN_DEPOSIT   = int(os.environ.get("MIN_DEPOSIT", "15"))
OWNER_ID      = int(os.environ.get("OWNER_ID", "8807310841"))
TG_CHANNEL    = os.environ.get("TG_CHANNEL", "https://t.me/+LY1YC_PBEl43NjU1")
REGISTER_LINK = os.environ.get("REGISTER_LINK", "https://broker-qx.pro/sign-up/?lid=2362141")
YOUTUBE       = os.environ.get("YOUTUBE", "")
COURSE_LINK   = os.environ.get("COURSE_LINK", "")
SUPPORT_USER  = os.environ.get("SUPPORT_USER", "@WOLF_BINARYSIGNALS")

# All reminder/onboarding media below is OPTIONAL and OFF by default (empty
# string). Telegram file_ids only work for the bot that originally received
# that file — Wolf's file_ids cannot be reused here. Send each real photo/
# video to this bot as the owner (OWNER_ID) and it will reply with the
# file_id to copy into the matching Railway variable below.
VIDEO_TUTORIAL = os.environ.get("VIDEO_TUTORIAL", "")
BONUS_PHOTO    = os.environ.get("BONUS_PHOTO", "")

def pe(eid, fb): return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'

# Unique emojis only in this section (full list is defined below near smart_reply)
E_LINK    = pe("5271604874419647061", "🔗")
E_EYES    = pe("5210956306952758910", "👀")
E_THUMBS  = pe("5337080053119336309", "👍")
E_GLOBE   = pe("5224450179368767019", "🌍")
E_NEW     = pe("5382357040008021292", "🆕")
E_PERSON  = pe("5217797330861826981", "👩‍💻")
E_HAND    = pe("5305522282695768654", "👇")

user_state: dict = {}

# ── PERSISTENT DB CONNECTION (reused across all calls, auto-reconnects) ──────
_db_conn = None

def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def _db_clean(value):
    if value is None:
        return None
    return "".join(ch for ch in str(value) if ch.isprintable()).strip()

def _parse_db_url():
    from urllib.parse import urlparse, unquote
    p = urlparse(_db_clean(DATABASE_URL))
    if not p.hostname or not p.username:
        raise RuntimeError("DATABASE_URL is malformed — copy it exactly from the Postgres service's Variables tab.")
    return dict(
        host=_db_clean(p.hostname),
        port=int(p.port or 5432),
        user=_db_clean(unquote(p.username or "")),
        password=_db_clean(unquote(p.password or "")),
        database=_db_clean((p.path or "/railway").lstrip("/")) or "railway",
    )

_DB_PARAMS = _parse_db_url()

# A single pg8000 connection is NOT safe for concurrent use. This lock makes sure
# two worker threads never run a query on the same connection at the same time —
# which is what causes random errors/garbled results under heavy traffic.
_db_lock = threading.Lock()

def get_db():
    """Return the shared connection, creating it once. No per-call ping —
    _db_run reconnects automatically if a query fails on a dropped connection."""
    global _db_conn
    if _db_conn is None:
        try:
            _db_conn = pg8000.native.Connection(**_DB_PARAMS, ssl_context=_make_ssl_ctx(), timeout=10)
        except Exception:
            _db_conn = pg8000.native.Connection(**_DB_PARAMS, ssl_context=None, timeout=10)
    return _db_conn

def _db_run(sql, **params):
    """Run a query under a lock, retrying up to 3 times with backoff."""
    global _db_conn
    with _db_lock:
        last_err = None
        for attempt in range(3):
            try:
                return get_db().run(sql, **params)
            except Exception as e:
                last_err = e
                _db_conn = None
                time.sleep(0.5 * (attempt + 1))
        raise last_err

# A deposit only counts toward VIP if it was recorded within this many days.
# Older deposits (e.g. someone who deposited a month ago and withdrew) show as $0,
# so the bot asks them to deposit again. Override with env var DEPOSIT_VALID_DAYS.
DEPOSIT_VALID_DAYS = int(os.environ.get("DEPOSIT_VALID_DAYS", "10"))

def db_get_trader(uid):
    try:
        rows = _db_run(
            "SELECT uid, deposit, "
            "(last_deposit_at IS NOT NULL AND last_deposit_at >= NOW() - (:days || ' days')::interval) AS recent "
            "FROM verified_traders WHERE uid = :uid",
            uid=uid, days=str(DEPOSIT_VALID_DAYS)
        )
        if rows:
            deposit = float(rows[0][1] or 0)
            recent = bool(rows[0][2])
            # Registered through our link = always recognized.
            # Deposit only counts if it happened within the window.
            return {"uid": rows[0][0], "deposit": deposit if recent else 0.0}
        return None
    except Exception as e:
        print(f"DB error: {e}")
        return None

def db_save_trader(uid, deposit=0.0, status="", country=""):
    """Returns True on success, False on failure (so callers can queue a retry)."""
    try:
        _db_run("""
            INSERT INTO verified_traders (uid, deposit, status, country, updated_at, last_deposit_at)
            VALUES (:uid, CAST(:dep AS DOUBLE PRECISION), :status, :country, NOW(),
                    CASE WHEN CAST(:dep AS DOUBLE PRECISION) > 0 THEN NOW() ELSE NULL END)
            ON CONFLICT (uid) DO UPDATE SET
                deposit = GREATEST(verified_traders.deposit, EXCLUDED.deposit),
                status = EXCLUDED.status,
                updated_at = NOW(),
                last_deposit_at = CASE WHEN EXCLUDED.deposit > 0 THEN NOW()
                                       ELSE verified_traders.last_deposit_at END
        """, uid=uid, dep=float(deposit), status=status, country=country)
        print(f"✅ Saved trader: {uid} dep=${deposit} status={status}")
        return True
    except Exception as e:
        print(f"DB save error: {e}")
        return False

def db_log_postback(uid, status, deposit, country):
    """Append-only audit record of every postback received. Never blocks the flow."""
    try:
        _db_run(
            "INSERT INTO postback_log (uid, status, deposit, country, received_at) "
            "VALUES (:uid, :status, :dep, :country, NOW())",
            uid=uid, status=status, dep=float(deposit), country=country
        )
    except Exception as e:
        print(f"Postback log error: {e}")

def db_save_reminder_state(chat_id, reminder_num, started_at):
    """Save reminder state to DB so it survives restarts"""
    try:
        _db_run("""
            INSERT INTO reminder_state (chat_id, reminder_num, started_at, updated_at)
            VALUES (:chat_id, :reminder_num, :started_at, NOW())
            ON CONFLICT (chat_id) DO UPDATE SET
                reminder_num = EXCLUDED.reminder_num,
                started_at = EXCLUDED.started_at,
                updated_at = NOW()
        """, chat_id=str(chat_id), reminder_num=reminder_num, started_at=started_at)
    except Exception as e:
        print(f"DB reminder save error: {e}")

def db_get_due_reminders(limit=300):
    try:
        return _db_run(
            "SELECT chat_id, reminder_num FROM reminder_state "
            "WHERE started_at <= NOW() - INTERVAL '10 hours' "
            "ORDER BY started_at LIMIT :lim", lim=int(limit)
        ) or []
    except Exception as e:
        print(f"DB due-reminders error: {e}")
        return []

def db_touch_reminder(chat_id, next_num):
    try:
        _db_run(
            "UPDATE reminder_state SET reminder_num = :n, started_at = NOW(), "
            "updated_at = NOW() WHERE chat_id = :c", n=int(next_num), c=str(chat_id)
        )
    except Exception as e:
        print(f"DB touch-reminder error: {e}")

def db_delete_reminder(chat_id):
    """Remove reminder state when user joins VIP"""
    try:
        _db_run("DELETE FROM reminder_state WHERE chat_id = :chat_id", chat_id=str(chat_id))
    except Exception as e:
        print(f"DB reminder delete error: {e}")

def db_create_tables():
    """Create tables if they don't exist"""
    try:
        _db_run("""
            CREATE TABLE IF NOT EXISTS verified_traders (
                uid TEXT PRIMARY KEY,
                deposit DOUBLE PRECISION DEFAULT 0,
                status TEXT DEFAULT '',
                country TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT NOW(),
                last_deposit_at TIMESTAMP
            )
        """)
        _db_run("ALTER TABLE verified_traders ADD COLUMN IF NOT EXISTS last_deposit_at TIMESTAMP")
        _db_run("""
            CREATE TABLE IF NOT EXISTS postback_log (
                id BIGSERIAL PRIMARY KEY,
                uid TEXT,
                status TEXT,
                deposit DOUBLE PRECISION DEFAULT 0,
                country TEXT DEFAULT '',
                received_at TIMESTAMP DEFAULT NOW()
            )
        """)
        _db_run("""
            CREATE TABLE IF NOT EXISTS reminder_state (
                chat_id TEXT PRIMARY KEY,
                reminder_num INTEGER DEFAULT 1,
                started_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        _db_run("CREATE INDEX IF NOT EXISTS idx_reminder_due ON reminder_state (started_at)")
        print("✅ Tables ready")
    except Exception as e:
        print(f"DB table creation error: {e}")

# ── POSTBACK DURABILITY ───────────────────────────────────────────────────────
# If the DB is down/restarting when a postback arrives, we must NOT lose it —
# the affiliate network will not resend. Failed saves go into this queue AND a
# JSON file on disk (survives a process crash within the same container).
# A background loop retries every 30s until the DB accepts them.
import json
from collections import deque

_PENDING_FILE = "/tmp/pending_postbacks.json"
_pending_postbacks = deque()

def _pending_persist():
    try:
        with open(_PENDING_FILE, "w") as f:
            json.dump(list(_pending_postbacks), f)
    except Exception as e:
        print(f"Pending-file write error: {e}")

def _pending_load():
    try:
        if os.path.exists(_PENDING_FILE):
            with open(_PENDING_FILE) as f:
                for item in json.load(f):
                    _pending_postbacks.append(item)
            if _pending_postbacks:
                print(f"♻️ Restored {len(_pending_postbacks)} pending postback(s) from disk")
    except Exception as e:
        print(f"Pending-file read error: {e}")

def queue_failed_postback(uid, deposit, status, country):
    _pending_postbacks.append({"uid": uid, "dep": deposit, "status": status, "country": country})
    _pending_persist()
    print(f"⚠️ Postback for uid={uid} queued for retry ({len(_pending_postbacks)} pending)")

async def postback_retry_loop():
    """Every 30s, try to flush queued postbacks into the DB."""
    while True:
        await asyncio.sleep(30)
        try:
            flushed = 0
            while _pending_postbacks:
                item = _pending_postbacks[0]
                ok = await asyncio.to_thread(
                    db_save_trader, item["uid"], item["dep"], item["status"], item["country"]
                )
                if not ok:
                    break  # DB still down; keep the rest queued
                _pending_postbacks.popleft()
                flushed += 1
            if flushed:
                _pending_persist()
                print(f"♻️ Flushed {flushed} queued postback(s) to DB")
        except Exception as e:
            print(f"Retry loop error: {e}")

# ── DAILY STATS (sent to OWNER_ID once a day) ────────────────────────────────
_stats = {"postbacks": 0, "save_fails": 0, "starts": 0, "verified": 0, "ftd": 0, "accounts_created": 0, "channel_joins": 0}

async def daily_summary_loop(bot):
    """Send daily report at 12:00 AM IST every day."""
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    while True:
        try:
            now = datetime.now(IST)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            wait_seconds = (midnight - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            snapshot = dict(_stats)
            pending = len(_pending_postbacks)
            await bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "📊 <b>Trading Wolf Bot — Daily Report</b>\n"
                    f"🕛 Last 24 hours (as of 12:00 AM IST)\n\n"
                    f"▶️ /start clicks: <b>{snapshot['starts']}</b>\n"
                    f"🆕 Accounts created (via our link): <b>{snapshot['accounts_created']}</b>\n"
                    f"✅ IDs verified + deposited (≥${MIN_DEPOSIT}): <b>{snapshot['verified']}</b>\n"
                    f"💰 FTD (first deposits): <b>{snapshot['ftd']}</b>\n"
                    f"📢 Joined public channel: <b>{snapshot['channel_joins']}</b>\n\n"
                    f"⚙️ System:\n"
                    f"  • DB save failures: {snapshot['save_fails']}\n"
                    f"  • Pending retries: {pending}"
                ),
                parse_mode=ParseMode.HTML
            )
            for k in _stats:
                _stats[k] = 0
        except Exception as e:
            print(f"Daily summary error: {e}")

USER_STATE_MAX = 150_000

def get_state(chat_id):
    if chat_id not in user_state:
        while len(user_state) >= USER_STATE_MAX:
            user_state.pop(next(iter(user_state)))
        user_state[chat_id] = {"step": "start", "trader_id": None, "deposit": 0.0, "reminder_task": None}
    return user_state[chat_id]

def cancel_reminder(state, chat_id=None):
    if state.get("reminder_task") and not state["reminder_task"].done():
        state["reminder_task"].cancel()
    if chat_id:
        try:
            asyncio.get_running_loop()
            asyncio.create_task(asyncio.to_thread(db_delete_reminder, chat_id))
        except RuntimeError:
            db_delete_reminder(chat_id)

def support_btn():
    return InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")

def register_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 REGISTER FREE NOW ⭐", url=AFFILIATE, style="danger")],
        [InlineKeyboardButton("🔑 I HAVE REGISTERED ✨", callback_data="registered", style="success")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton("✉️ CONTACT SUPPORT 24/7", url=SUPPORT, style="primary")],
    ])

def deposit_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Claim 50% Bonus NOW", callback_data="claim_bonus", style="success")],
        [InlineKeyboardButton("📹 How To Deposit (Tutorial)", callback_data="tutorial", style="primary")],
        [InlineKeyboardButton("🔄 I Have Deposited (Re-Check)", callback_data="deposited", style="danger")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
    ])

def reject_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Register With Our Link", url=AFFILIATE, style="danger")],
        [InlineKeyboardButton("🔄 Try Again With Correct ID", callback_data="try_again", style="success")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
    ])

def reminder_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Create Free Account Now", url=AFFILIATE, style="success")],
        [InlineKeyboardButton("🔥 Click Here To Join VIP", url=AFFILIATE, style="danger")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
    ])

def bonus_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Deposit Now", url=AFFILIATE, style="danger")],
        [InlineKeyboardButton("📹 How To Deposit (Tutorial)", callback_data="tutorial", style="primary")],
        [InlineKeyboardButton("✅ I Have Deposited", callback_data="deposited", style="success")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
    ])

def vip_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 JOIN VIP SIGNALS GROUP 🏆", url=VIP_LINK, style="danger")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
    ])

def support_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [support_btn()],
    ])

def recheck_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Deposit Now", url=AFFILIATE, style="danger")],
        [InlineKeyboardButton("🎁 Claim 50% Bonus NOW", callback_data="claim_bonus", style="success")],
        [InlineKeyboardButton("📹 How To Deposit (Tutorial)", callback_data="tutorial", style="primary")],
        [InlineKeyboardButton("🔄 I Have Deposited (Re-Check)", callback_data="deposited", style="success")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
    ])



# ─── REMINDER CAPTIONS ────────────────────────────────────────────
REM1_CAP = os.environ.get("REM1_CAPTION", '<b>📌 ZERO TO HERO JOURNEY 📌\n❗️❗️❗️❗️❗️❗️❗️❗️\n\nWant to see how our members go from zero to confident traders?\n\n👆👆 Ask us for the journey video\n( MUST WATCH ) 🔥 📣</b>')
REM2_CAP = os.environ.get("REM2_CAPTION", f'<b>🚨 DON\'T SKIP THIS 🚨\n👀 Check today\'s session results in our channel before you decide\n\n📊 See today\'s live trading session results here:\n{TG_CHANNEL}\n\n💬 Want to join VIP?\n✉️ Message "VIP" ➡️ {SUPPORT_USER}</b>')
REM3_CAP = os.environ.get("REM3_CAPTION", '<b>🦁 SIGNALS THAT WORK 🏆\n❗️❗️❗️❗️❗️❗️❗️❗️\n\n👆👆 Ask us for a walkthrough of how our VIP signals work 🔥🚨</b>')
REM4_CAP = os.environ.get("REM4_CAPTION", f'<b>💵📈 Grow step by step, following our VIP signals\n\n☄️ Check our channel for member results\n\n▶️ See real member feedback in our channel 👆👆\n\nDon\'t wait around — the next success story could be yours 🚀\n\n✉️ JOIN NOW — Message "VIP"\n📱 MESSAGE HERE ➡️ {SUPPORT_USER}</b>')
REM5_CAP = os.environ.get("REM5_CAPTION", f'<b>👍 Bhai, imagine your parents genuinely proud of what you\'ve built 🫂\n\nThat kind of pride doesn\'t come overnight — it comes from steady effort and smart decisions 💵\n\nIf you\'re serious about learning to trade properly and want high quality signals,\nmessage "VIP" ➡️ {SUPPORT_USER}</b>')
REM_BTN_PLAY = '▶️ Watch Video Click Here'
REM_BTN_KEY = '🔑 Register Your Account'
REM_BTN_MAIL = '✉️ Contact Support 24/7'

SEND_SEMAPHORE = asyncio.Semaphore(15)

# Chats that have permanently disabled voice/video-note messages (a Telegram privacy
# setting, not something a retry can fix). Once we see this for a chat we stop
# sending them video notes forever, instead of failing on the same send every single
# reminder cycle. In-memory only (resets on restart) — cheap, and worst case we just
# re-detect and re-skip it once.
_voice_note_blocked: set = set()

def _is_voice_forbidden(e) -> bool:
    msg = str(e).lower()
    return "voice_messages_forbidden" in msg or "video_messages_forbidden" in msg

# Reminder media (photo/video) is sent from hardcoded file_ids. If the original
# upload is ever deleted on Telegram's side, that file_id becomes permanently
# invalid - every chat's reminder cycle would otherwise hit the same "Wrong
# file identifier" error forever, each one logging a full traceback. Once we
# see that for a given file_id, skip it everywhere (no more wasted API calls,
# no more log spam) until someone fixes the file_id in the code.
_broken_file_ids: set = set()

def _is_bad_file_id(e) -> bool:
    msg = str(e).lower()
    return "wrong file identifier" in msg or "wrong remote file identifier" in msg

async def _send_reminder_media(chat_id, file_id, label, send_coro_factory):
    """Send one piece of reminder media, or skip it silently once its file_id
    is already known to be invalid. send_coro_factory is a zero-arg callable
    returning the awaitable that performs the actual send."""
    if file_id in _broken_file_ids:
        return
    try:
        await send_coro_factory()
    except Exception as e:
        if _is_bad_file_id(e):
            _broken_file_ids.add(file_id)
            print(f"🚫 {label} file_id is invalid ({e}) — skipping it for every chat until it's replaced")
        else:
            _reminder_send_error(chat_id, label, e)

async def send_one_reminder(chat_id, bot, reminder_num):
    """Send a single reminder by number (1-5)"""
    async with SEND_SEMAPHORE:
        return await _send_one_reminder_inner(chat_id, bot, reminder_num)

def _reminder_send_error(chat_id, label, e):
    """Log a reminder send failure. If the failure is PERMANENT (user blocked
    the bot, account deleted, chat gone), remove the user from the reminder
    cycle so we never waste sends retrying them every 10 hours forever."""
    msg = str(e)
    print(f"[chat {chat_id}] {label} error: {e}")
    permanent = (isinstance(e, Forbidden)
                 or "blocked by the user" in msg or "user is deactivated" in msg
                 or "chat not found" in msg.lower() or "bot was kicked" in msg)
    if permanent:
        try:
            st = user_state.get(chat_id)
            if st is not None:
                st["step"] = "done"
            asyncio.create_task(asyncio.to_thread(db_delete_reminder, chat_id))
            print(f"🚫 chat {chat_id} unreachable permanently — removed from reminder cycle")
        except Exception as cleanup_err:
            print(f"Cleanup error for {chat_id}: {cleanup_err}")


async def _send_one_reminder_inner(chat_id, bot, reminder_num):
    r1_cap = REM1_CAP
    r2_cap = REM2_CAP
    r3_cap = REM3_CAP
    r4_cap = REM4_CAP
    r5_cap = REM5_CAP
    yt = "https://youtu.be/q1a8FZ8T4XU?si=bMvgGhQ1Ru6nLayx"
    btn_13 = InlineKeyboardMarkup([
        [InlineKeyboardButton(REM_BTN_PLAY, url=yt, style="danger")],
        [InlineKeyboardButton(REM_BTN_KEY, callback_data="registered", style="success")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton(REM_BTN_MAIL, url=SUPPORT, style="primary")],
    ])
    btn_245 = InlineKeyboardMarkup([
        [InlineKeyboardButton(REM_BTN_KEY, callback_data="registered", style="success")],
        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
        [InlineKeyboardButton(REM_BTN_MAIL, url=SUPPORT, style="primary")],
    ])
    if reminder_num == 1:
        photo_id = os.environ.get("REM1_PHOTO", "")
        if photo_id:
            await _send_reminder_media(
                chat_id, photo_id, "Reminder 1 PHOTO",
                lambda: bot.send_photo(chat_id=chat_id, photo=photo_id, caption=r1_cap, parse_mode="HTML"),
            )
        video_note_id = os.environ.get("REM1_VIDEONOTE", "")
        if video_note_id and chat_id not in _voice_note_blocked and video_note_id not in _broken_file_ids:
            try:
                await bot.send_video_note(chat_id=chat_id, video_note=video_note_id, reply_markup=btn_13)
            except Exception as e:
                if _is_voice_forbidden(e):
                    _voice_note_blocked.add(chat_id)
                    print(f"🔇 chat {chat_id} has voice/video messages disabled — skipping video notes from now on")
                elif _is_bad_file_id(e):
                    _broken_file_ids.add(video_note_id)
                    print(f"🚫 Reminder 1 VIDEO_NOTE file_id is invalid ({e}) — skipping it for every chat until it's replaced")
                else:
                    _reminder_send_error(chat_id, "Reminder 1 VIDEO_NOTE", e)
    elif reminder_num == 2:
        video_id = os.environ.get("REM2_VIDEO", "")
        if video_id:
            await _send_reminder_media(
                chat_id, video_id, "Reminder 2 VIDEO",
                lambda: bot.send_video(chat_id=chat_id, video=video_id, caption=r2_cap, parse_mode="HTML", reply_markup=btn_245),
            )
    elif reminder_num == 3:
        photo_id = os.environ.get("REM3_PHOTO", "")
        if photo_id:
            await _send_reminder_media(
                chat_id, photo_id, "Reminder 3 PHOTO",
                lambda: bot.send_photo(chat_id=chat_id, photo=photo_id, caption=r3_cap, parse_mode="HTML"),
            )
        video_note_id = os.environ.get("REM3_VIDEONOTE", "")
        if video_note_id and chat_id not in _voice_note_blocked and video_note_id not in _broken_file_ids:
            try:
                btn_3 = InlineKeyboardMarkup([
                    [InlineKeyboardButton(REM_BTN_KEY, callback_data="registered", style="success")],
                    [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
                    [InlineKeyboardButton(REM_BTN_MAIL, url=SUPPORT, style="primary")],
                ])
                await bot.send_video_note(chat_id=chat_id, video_note=video_note_id, reply_markup=btn_3)
            except Exception as e:
                if _is_voice_forbidden(e):
                    _voice_note_blocked.add(chat_id)
                    print(f"🔇 chat {chat_id} has voice/video messages disabled — skipping video notes from now on")
                elif _is_bad_file_id(e):
                    _broken_file_ids.add(video_note_id)
                    print(f"🚫 Reminder 3 VIDEO_NOTE file_id is invalid ({e}) — skipping it for every chat until it's replaced")
                else:
                    _reminder_send_error(chat_id, "Reminder 3 VIDEO_NOTE", e)
    elif reminder_num == 4:
        video_id = os.environ.get("REM4_VIDEO", "")
        if video_id:
            await _send_reminder_media(
                chat_id, video_id, "Reminder 4 VIDEO",
                lambda: bot.send_video(chat_id=chat_id, video=video_id, caption=r4_cap, parse_mode="HTML", reply_markup=btn_245),
            )
    elif reminder_num == 5:
        photo_id = os.environ.get("REM5_PHOTO", "")
        if photo_id:
            await _send_reminder_media(
                chat_id, photo_id, "Reminder 5 PHOTO",
                lambda: bot.send_photo(chat_id=chat_id, photo=photo_id, caption=r5_cap, parse_mode="HTML", reply_markup=btn_245),
            )

async def reminder_scheduler_loop(bot):
    """ONE DB-driven loop for ALL users. Every 60s: who is due -> send -> reset clock.
    Flat memory at any scale; restarts need no restoration; no stampedes."""
    print("Reminder scheduler loop started (DB-driven).")
    while True:
        try:
            due = await asyncio.to_thread(db_get_due_reminders, 300)
            for chat_id_str, reminder_num in due:
                chat_id = int(chat_id_str)
                state = user_state.get(chat_id)
                if state is not None and state.get("step") == "done":
                    await asyncio.to_thread(db_delete_reminder, chat_id)
                    continue
                next_num = (int(reminder_num) % 4) + 1
                await asyncio.to_thread(db_touch_reminder, chat_id, next_num)
                asyncio.create_task(send_one_reminder(chat_id, bot, int(reminder_num)))
        except Exception as e:
            print(f"Scheduler loop error: {e}")
        await asyncio.sleep(60)


async def _safe_step(chat_id, label, coro):
    """Run one send in the onboarding sequence. Previously, ANY failure here
    (even a one-off network blip) aborted the entire ~9-minute sequence — and
    since reminders only get scheduled at the very end, that user silently
    never received another message again. Now: log it, skip that one send,
    and keep the sequence going. Forbidden (user blocked the bot) still
    propagates up so the caller can stop the whole sequence for that user."""
    try:
        return await coro
    except Forbidden:
        raise
    except Exception as e:
        print(f"[chat {chat_id}] start-sequence step '{label}' failed, continuing: {e}")
        return None

async def run_start_sequence(chat_id, bot, state):
    try:
        # ── SEQUENCE 1: Immediate — exact order: sticker → photo → video note (buttons)
        _seq1_sticker = os.environ.get("SEQ1_STICKER", "")
        if _seq1_sticker:
            await _safe_step(chat_id, "seq1 sticker", bot.send_sticker(
                chat_id=chat_id,
                sticker=_seq1_sticker
            ))
        _seq1_photo = os.environ.get("SEQ1_PHOTO", "")
        if _seq1_photo:
            await _safe_step(chat_id, "seq1 photo", bot.send_photo(
            chat_id=chat_id,
            photo=_seq1_photo,
            caption=(
                f"<b>🏆PAISA KAMANA HAI? TOH ABHI JOIN KARO! 💵\n\n"
                f"🫴Mere Free Channel Mein Roz Milta Hai:\n\n"
                f"✅Rozana 10-20 Signals 📈\n"
                f"✅Non-Martingale Trades 🎯\n"
                f"✅4-5 Trading Sessions Har Din 🔥\n"
                f"✅High-Quality Trading Signals 🏆\n"
                f"✅Beginners Ke Liye Bhi Aasan\n\n"
                f"🫴FREE CHANNEL JOIN KARO 🫴\n\n"
                f"{TG_CHANNEL}\n"
                f"{TG_CHANNEL}\n"
                f"{TG_CHANNEL}\n\n"
                f"🚨Jaldi Join Karo Aur Agla Signal Miss Mat Karo!\n\n"
                f"@WOLF_BINARYSIGNALS🐺</b>"
            ),
            parse_mode=ParseMode.HTML
        ))
        _seq1_vn = os.environ.get("SEQ1_VIDEONOTE", "")
        if _seq1_vn and chat_id not in _voice_note_blocked:
            try:
                await bot.send_video_note(
                    chat_id=chat_id,
                    video_note=_seq1_vn,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📈 FREE VIP GROUP", url=VIP_LINK, style="danger")],
                        [InlineKeyboardButton("🎯 JOIN LOSS RECOVERY", url=VIP_LINK, style="primary")],
                        [InlineKeyboardButton("💬 CONTACT SUPPORT 24/7", url=SUPPORT, style="success")],
                    ])
                )
            except Forbidden:
                raise
            except Exception as e:
                if _is_voice_forbidden(e):
                    _voice_note_blocked.add(chat_id)
                    print(f"🔇 chat {chat_id} has voice/video messages disabled — skipping video notes from now on")
                else:
                    print(f"[chat {chat_id}] start-sequence step 'seq1 video_note' failed, continuing: {e}")

        # ── WAIT 3 MINUTES ────────────────────────────────────────────────
        await asyncio.sleep(180)

        # ── SEQUENCE 2: +3 min ────────────────────────────────────────────
        # Text message 1
        await _safe_step(chat_id, "seq2 text1", bot.send_message(
            chat_id=chat_id,
            text=(
                f"<b>Hello {E_TROPHY} Are You Ready To Earn Money With Trading Without Experience\n\n"
                f"{E_CHART} I Helped many members To Start EARNING {E_ROCKET}\n\n"
                f"{E_WARN} I Shared The Result Of My Client Earning With Me {E_HAND}</b>"
            ),
            parse_mode=ParseMode.HTML
        ))

        # Photo/Video Album — set MEDIA_GROUP_1..10 (any subset) once you have real proof media
        _mg_ids = [os.environ.get(f"MEDIA_GROUP_{i}", "") for i in range(1, 11)]
        _mg_items = []
        for _i, _mid in enumerate(_mg_ids):
            if not _mid:
                continue
            if _mid.startswith("v:"):
                _mg_items.append(InputMediaVideo(media=_mid[2:]))
            else:
                _mg_items.append(InputMediaPhoto(media=_mid))
        if len(_mg_items) >= 2:
            await _safe_step(chat_id, "seq2 media_group", bot.send_media_group(chat_id=chat_id, media=_mg_items))

        # Text message with button (name/country)
        await _safe_step(chat_id, "seq2 text2", bot.send_message(
            chat_id=chat_id,
            text=(
                f"<b>{E_PERSON} Bro, What Is Your Name And What's Your Country? {E_GLOBE}\n\n"
                f"{E_NEW} It Will Help Us To Understand Each Other Better {E_TROPHY}</b>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=support_keyboard()
        ))

        # ── WAIT 3 MINUTES ────────────────────────────────────────────────
        await asyncio.sleep(180)

        # ── SEQUENCE 3: +3 min — Free Channel Promo ───────────────────────
        _seq3_photo = os.environ.get("SEQ3_PHOTO", "")
        if _seq3_photo:
            await _safe_step(chat_id, "seq3 photo", bot.send_photo(
            chat_id=chat_id,
            photo=_seq3_photo,
            caption=(
                f"<b>🐺PAISA KAMANA HAI? TOH ABHI JOIN KARO! 💵🔥\n\n"
                f"🫴FREE SIGNALS GROUP MEIN MILTA HAI:\n\n"
                f"✅Roz 10-20 Signals 📈\n"
                f"✅Bina Martingale Ke Trades 🎯\n"
                f"✅Roz 4-5 Sessions 🔥\n"
                f"✅Asaan Entry & Exit Levels 📊\n"
                f"✅Beginners Ke Liye Bilkul Easy 👌\n"
                f"✅Risk Control Ka Proper Gyaan 💡\n\n"
                f"🫴FREE CHANNEL JOIN KARO 🫴\n\n"
                f"{TG_CHANNEL}\n{TG_CHANNEL}\n{TG_CHANNEL}\n\n"
                f"@WOLF_BINARYSIGNALS🐺</b>"
            ),
            parse_mode=ParseMode.HTML
        ))
        _seq3_vn = os.environ.get("SEQ3_VIDEONOTE", "")
        if _seq3_vn and chat_id not in _voice_note_blocked:
            try:
                await bot.send_video_note(
                    chat_id=chat_id,
                    video_note=_seq3_vn,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="danger")],
                        [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
                    ])
                )
            except Forbidden:
                raise
            except Exception as e:
                if _is_voice_forbidden(e):
                    _voice_note_blocked.add(chat_id)
                    print(f"🔇 chat {chat_id} has voice/video messages disabled — skipping video notes from now on")
                else:
                    print(f"[chat {chat_id}] start-sequence step 'seq3 video_note' failed, continuing: {e}")

        # ── WAIT 3 MINUTES ────────────────────────────────────────────────
        await asyncio.sleep(180)

        # ── SEQUENCE 4: +3 min ────────────────────────────────────────────
        # Registration tutorial video
        _seq4_video = os.environ.get("SEQ4_VIDEO", "")
        _seq4_text = (
            f"<b>🐺VIP MEMBERS KO MILTA HAI PREMIUM SIGNALS! 🚀\n\n"
            f"✅ 5-10+ Signals Ke Saat\n"
            f"✅ 4-5 Sessions Roz Hothe Hai\n"
            f"✅ Proper Entry & Exit Levels\n"
            f"✅ Dedicated 24/7 Support\n\n"
            f"🎁 Pehle FREE Channel Join Karo:\n"
            f"{TG_CHANNEL}\n{TG_CHANNEL}\n{TG_CHANNEL}\n\n"
            f"@WOLF_BINARYSIGNALS 🐺</b>"
        )
        if _seq4_video:
            await _safe_step(chat_id, "seq4 video", bot.send_video(
                chat_id=chat_id,
                video=_seq4_video,
                caption=_seq4_text,
                parse_mode=ParseMode.HTML,
                reply_markup=register_keyboard()
            ))
        else:
            await _safe_step(chat_id, "seq4 text", bot.send_message(
                chat_id=chat_id,
                text=_seq4_text,
                parse_mode=ParseMode.HTML,
                reply_markup=register_keyboard()
            ))

        from datetime import datetime, timezone
        await asyncio.to_thread(db_save_reminder_state, chat_id, 1, datetime.now(timezone.utc).isoformat())
        # The DB row alone schedules future reminders (see reminder_scheduler_loop).

    except Forbidden:
        # User blocked the bot mid-sequence — stop quietly, cancel reminders.
        print(f"🚫 User {chat_id} blocked the bot during start sequence — stopping")
        await asyncio.to_thread(db_delete_reminder, chat_id)
        user_state.pop(chat_id, None)
    except Exception as e:
        import traceback
        print(f"START SEQ ERROR: {e}")
        traceback.print_exc()

async def verify_id_then_respond(uid, chat_id, bot):
    state = get_state(chat_id)
    cancel_reminder(state, chat_id)

    msg = await bot.send_message(
        chat_id=chat_id,
        text=f"<b>{E_EYES} Verifying ID <code>{uid}</code>... Please wait!</b>",
        parse_mode=ParseMode.HTML
    )

    # Run DB lookup in a thread so it doesn't block the event loop
    trader = await asyncio.to_thread(db_get_trader, uid)
    if not trader:
        # Wait 1 second for postback, then check once more
        await asyncio.sleep(1)
        trader = await asyncio.to_thread(db_get_trader, uid)

    if not trader:
        state["step"] = "awaiting_id"
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg.message_id,
            text=(
                f"<b>{E_CROSS} Bro, this account is NOT registered through my link! {E_WARN}\n\n"
                f"Please re-check and send the correct Trader ID. {E_EYES}\n\n"
                f"{E_CHAT} Contact us anytime — our team is available 24/7! {E_CLOCK}</b>"
            ),
            parse_mode=ParseMode.HTML, reply_markup=reject_keyboard()
        )
        from datetime import datetime, timezone
        await asyncio.to_thread(db_save_reminder_state, chat_id, 1, datetime.now(timezone.utc).isoformat())
        return

    dep = trader["deposit"]
    state["trader_id"] = uid
    state["deposit"] = dep

    if dep >= MIN_DEPOSIT:
        _stats["verified"] += 1
        _stats["channel_joins"] += 1
        state["step"] = "done"
        cancel_reminder(state, chat_id)
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg.message_id,
            text=f"<b>{E_CHECK} ID <code>{uid}</code> verified! {E_PARTY} Deposit confirmed! {E_ROCKET}</b>",
            parse_mode=ParseMode.HTML
        )
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"<b>{E_PARTY} WELCOME TO VIP! {E_CROWN}\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"{E_TROPHY} You are now a verified VIP member!\n\n"
                f"{E_FIRE} Join our Exclusive VIP Signals Group NOW:\n\n"
                f"{E_DIAMOND} {VIP_LINK} {E_DIAMOND}\n\n"
                f"{E_CHART} Daily 10-20 Sureshot Trades\n"
                f"{E_MONEY} Daily 5-10 Compounding Signals\n"
                f"{E_STAR} All Trades 100% NON-MTG\n\n"
                f"{E_THUMBS} Welcome to the winning team! {E_TROPHY}</b>"
            ),
            parse_mode=ParseMode.HTML, reply_markup=vip_keyboard()
        )
    else:
        state["step"] = "awaiting_deposit"
        # If deposit exists but less than minimum, tell them directly
        if dep > 0 and dep < MIN_DEPOSIT:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=msg.message_id,
                text=(
                    f"<b>{E_CHECK} ID <code>{uid}</code> verified! {E_WARN}\n\n"
                    f"Your current balance: <b>${dep:.2f}</b>\n\n"
                    f"{E_MONEY} You need to deposit minimum <b>${MIN_DEPOSIT}</b> to unlock VIP!\n\n"
                    f"Please deposit <b>${MIN_DEPOSIT - dep:.2f} more</b> and click Re-Check {E_HAND}</b>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 Deposit Now", url=AFFILIATE, style="danger")],
                    [InlineKeyboardButton("📹 How To Deposit (Tutorial)", callback_data="tutorial", style="primary")],
                    [InlineKeyboardButton("🔄 I Have Deposited (Re-Check)", callback_data="deposited", style="success")],
                    [InlineKeyboardButton("💰 JOIN FREE CHANNEL", url=TG_CHANNEL, style="primary")],
                    [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
                ])
            )
            return
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg.message_id,
            text=(
                f"<b>{E_CHECK} ID <code>{uid}</code> verified! {E_WARN}\n\n"
                f"ACCOUNT LINKED — $0.00 BALANCE\n\n"
                f"{E_EYES} Found your Quotex ID but balance is ZERO!\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"{E_EYES} Deposit at least ${MIN_DEPOSIT} and click Re-Check to unlock VIP {E_CHECK}</b>"
            ),
            parse_mode=ParseMode.HTML, reply_markup=deposit_keyboard()
        )



async def preview_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Preview any reminder instantly - /preview1 through /preview5"""
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    cmd = update.message.text.strip().lower()
    num = int(cmd.replace("/preview", "")) if cmd.replace("/preview", "").isdigit() else 0
    if num < 1 or num > 5:
        await update.message.reply_text("Use /preview1 to /preview5")
        return
    await update.message.reply_text(f"Sending reminder {num} preview...")
    await send_one_reminder(update.effective_chat.id, context.bot, num)

async def clear_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /clearreminders — wipes all stuck reminder states from DB"""
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        return
    try:
        rows = _db_run("SELECT chat_id FROM reminder_state")
        count = len(rows) if rows else 0
        _db_run("DELETE FROM reminder_state")
        await update.message.reply_text(f"✅ Cleared {count} reminder(s) from DB. All stuck reminders removed!")
        print(f"✅ Admin cleared {count} reminders from DB")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only /stats — live health check from Telegram."""
    if update.effective_user is None or update.effective_user.id != OWNER_ID:
        return
    try:
        traders = (await asyncio.to_thread(_db_run, "SELECT COUNT(*) FROM verified_traders"))[0][0]
        def _q_deps():
            return _db_run("SELECT COUNT(*) FROM verified_traders WHERE deposit >= :m", m=MIN_DEPOSIT)
        def _q_recent():
            return _db_run(
                "SELECT COUNT(*) FROM verified_traders "
                "WHERE last_deposit_at >= NOW() - (:d || ' days')::interval", d=str(DEPOSIT_VALID_DAYS))
        deps = (await asyncio.to_thread(_q_deps))[0][0]
        recent = (await asyncio.to_thread(_q_recent))[0][0]
        logs = (await asyncio.to_thread(_db_run, "SELECT COUNT(*) FROM postback_log"))[0][0]
        db_ok = "✅ Connected"
    except Exception as e:
        traders = deps = recent = logs = "?"
        db_ok = f"❌ {e}"
    await update.message.reply_text(
        "📊 <b>Bot Stats</b>\n"
        f"Database: {db_ok}\n"
        f"Total traders saved: {traders}\n"
        f"Traders with deposit ≥ ${MIN_DEPOSIT}: {deps}\n"
        f"Deposits valid now (last {DEPOSIT_VALID_DAYS} days): {recent}\n"
        f"Total postbacks logged: {logs}\n"
        f"Pending retry queue: {len(_pending_postbacks)}\n"
        f"Today so far — postbacks: {_stats['postbacks']}, fails: {_stats['save_fails']}, "
        f"starts: {_stats['starts']}, verified: {_stats['verified']}, FTD: {_stats['ftd']}",
        parse_mode=ParseMode.HTML
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    cancel_reminder(state, chat_id)
    state.update({"step": "start", "trader_id": None, "deposit": 0.0, "reminder_task": None})
    _stats["starts"] += 1
    # Reminder is started ONLY inside run_start_sequence() after intro completes
    asyncio.create_task(run_start_sequence(chat_id, context.bot, state))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.message is None:
        return
    chat_id = query.message.chat_id
    state = get_state(chat_id)

    if query.data in ("registered", "reg"):
        _stats["accounts_created"] += 1
        cancel_reminder(state, chat_id)
        state["step"] = "awaiting_id"
        _reg_photo = os.environ.get("REGISTERED_STEP_PHOTO", "")
        _reg_text = (
            f"<b>{E_PARTY} Congratulations! You're just one step away! {E_FIRE}\n\n"
            f"━━━━━━━━━━━━\n\n"
            f"{E_EYES} Follow these steps to find your Trader ID:\n\n"
            f"1️⃣ Open your <b>Quotex account</b>\n"
            f"2️⃣ Go to <b>My Account</b>\n"
            f"3️⃣ You will see your <b>Trader ID</b> there\n"
            f"4️⃣ Reply with that <b>8-digit code</b> {E_HAND}</b>"
        )
        if _reg_photo:
            await context.bot.send_photo(
                chat_id=chat_id, photo=_reg_photo, caption=_reg_text,
                parse_mode=ParseMode.HTML, reply_markup=support_keyboard()
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=_reg_text,
                parse_mode=ParseMode.HTML, reply_markup=support_keyboard()
            )

    elif query.data == "try_again":
        state["step"] = "awaiting_id"
        _reg_photo = os.environ.get("REGISTERED_STEP_PHOTO", "")
        _reg_text = (
            f"<b>🔄 Please send your correct Trader ID {E_EYES}\n\n"
            f"━━━━━━━━━━━━\n\n"
            f"{E_EYES} Follow these steps to find your Trader ID:\n\n"
            f"1️⃣ Open your <b>Quotex account</b>\n"
            f"2️⃣ Go to <b>My Account</b>\n"
            f"3️⃣ You will see your <b>Trader ID</b> there\n"
            f"4️⃣ Reply with that <b>8-digit code</b> {E_HAND}</b>"
        )
        if _reg_photo:
            await context.bot.send_photo(
                chat_id=chat_id, photo=_reg_photo, caption=_reg_text,
                parse_mode=ParseMode.HTML, reply_markup=support_keyboard()
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=_reg_text,
                parse_mode=ParseMode.HTML, reply_markup=support_keyboard()
            )

    elif query.data == "tutorial":
        _tut_text = (
            f"<b>{E_MONEY} How To Deposit Tutorial {E_CHART}\n\n"
            f"👆 Watch this video to learn how to deposit on Quotex!</b>"
        )
        _tut_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Deposit Now", url=AFFILIATE, style="danger")],
            [InlineKeyboardButton("🔄 I Have Deposited (Re-Check)", callback_data="deposited", style="success")],
            [InlineKeyboardButton("✉️ Contact Support 24/7", url=SUPPORT, style="primary")],
        ])
        if VIDEO_TUTORIAL:
            await context.bot.send_video(
                chat_id=chat_id, video=VIDEO_TUTORIAL, caption=_tut_text,
                parse_mode=ParseMode.HTML, reply_markup=_tut_kb,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=_tut_text,
                parse_mode=ParseMode.HTML, reply_markup=_tut_kb,
            )

    elif query.data == "claim_bonus":
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=BONUS_PHOTO,
            caption=(
                f"<b>50% DEPOSIT BONUS CODE FREE !! \n"
                f"👑👑👑\n\n"
                f"✅CREATE QUOTEX ACCOUNT WITH THIS LINK ⬇️\n\n"
                f"🔗{AFFILIATE}\n\n"
                f"🔗{AFFILIATE}\n\n"
                f"✅Deposit minimum $150 & Get 50% Deposit Bonus 🤑🤤\n\n"
                f'Just Enter the promo code -&gt; "WOLF50" at the time of Deposit\n\n'
                f"⚠️ Promo codes can only be used by accounts created with this Link\n"
                f"⬇️\n{AFFILIATE}</b>"
            ),
            parse_mode=ParseMode.HTML, reply_markup=bonus_keyboard()
        )

    elif query.data == "deposited":
        uid = state.get("trader_id")
        if not uid:
            await query.message.reply_text(
                f"<b>{E_WARN} Please send your Trader ID first! {E_HAND}</b>",
                parse_mode=ParseMode.HTML, reply_markup=support_keyboard()
            )
            return
        trader = await asyncio.to_thread(db_get_trader, uid)
        dep = trader["deposit"] if trader else 0.0
        state["deposit"] = dep
        if dep >= MIN_DEPOSIT:
            state["step"] = "done"
            cancel_reminder(state, chat_id)
            await query.message.reply_text(
                f"<b>{E_PARTY} Deposit Confirmed! WELCOME TO VIP! {E_CROWN}\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"{E_TROPHY} You are now a verified VIP member!\n\n"
                f"{E_FIRE} Join Exclusive VIP Signals Group NOW:\n\n"
                f"{E_DIAMOND} {VIP_LINK} {E_DIAMOND}\n\n"
                f"{E_THUMBS} Welcome to the winning team! {E_TROPHY}</b>",
                parse_mode=ParseMode.HTML, reply_markup=vip_keyboard()
            )
        else:
            await query.message.reply_text(
                f"<b>{E_WARN} Bro, your balance shows <b>${dep:.2f}</b>! {E_CROSS}\n\n"
                f"ID: <code>{uid}</code>\n\n"
                f"{E_MONEY} Please deposit <b>${MIN_DEPOSIT} or more</b> and click Re-Check! {E_HAND}</b>",
                parse_mode=ParseMode.HTML, reply_markup=recheck_keyboard()
            )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture file IDs sent directly to the bot — OWNER ONLY.
    Without this guard, every user who sends a photo/video gets a 'FILE ID' reply."""
    if update.effective_user is None or update.effective_user.id != OWNER_ID:
        return
    if update.message.photo:
        fid = update.message.photo[-1].file_id
        await update.message.reply_text("PHOTO FILE ID:\n" + fid)
    elif update.message.video:
        fid = update.message.video.file_id
        await update.message.reply_text("VIDEO FILE ID:\n" + fid)
    elif update.message.video_note:
        fid = update.message.video_note.file_id
        await update.message.reply_text("VIDEO NOTE FILE ID:\n" + fid)
    elif update.message.document:
        fid = update.message.document.file_id
        await update.message.reply_text("DOCUMENT FILE ID:\n" + fid)
    elif update.message.sticker:
        fid = update.message.sticker.file_id
        await update.message.reply_text("STICKER FILE ID:\n" + fid)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None or not update.message.text:
        return
    chat_id = update.effective_chat.id
    state = get_state(chat_id)
    text = update.message.text.strip()

    if state["step"] == "awaiting_id":
        if not text.isdigit():
            reply = await ask_gemini(chat_id, text)
            await update.message.reply_text(reply, parse_mode=ParseMode.HTML)
            return
        state["step"] = "checking"
        asyncio.create_task(verify_id_then_respond(text, chat_id, context.bot))
    elif state["step"] == "awaiting_deposit":
        if text.isdigit():
            state["step"] = "checking"
            asyncio.create_task(verify_id_then_respond(text, chat_id, context.bot))
        else:
            reply = await ask_gemini(chat_id, text)
            await update.message.reply_text(reply, parse_mode=ParseMode.HTML)
    else:
        reply = await ask_gemini(chat_id, text)
        await update.message.reply_text(reply, parse_mode=ParseMode.HTML)

# ── PREMIUM EMOJI HELPER (full set) ───────────────────────────────────────────
# pe() is defined near the top of the file
# All premium emojis:
E_CLOCK   = pe("5431807687136395567", "⏰")
E_SUN     = pe("5402477260982731644", "☀️")
E_LEGAL   = pe("5400250414929041085", "⚖️")
E_WARN    = pe("5420323339723881652", "⚠️")
E_BOLT    = pe("5411590687663608498", "⚡")
E_CHECK   = pe("5206607081334906820", "✅")
E_CROSS   = pe("5210952531676504517", "❌")
E_THUMB   = pe("5465465194056525619", "👍")
E_NIGHT   = "🌃"   # no custom id provided
E_SUNSET  = "🌅"   # no custom id provided
E_MOON    = pe("5449569374065152798", "🌙")
E_STAR    = pe("5438496463044752972", "⭐")
E_CLOUD   = "🌤️"  # no custom id provided
E_GIFT    = pe("5449800250032143374", "🎁")
E_PARTY   = pe("5461151367559141950", "🎉")
E_GAME    = pe("5319247469165433798", "🎮")
E_TARGET  = pe("6185808707985608949", "🎯")
E_TROPHY  = pe("5413566144986503832", "🏆")
E_HOME    = pe("5416041192905265756", "🏠")
E_BANK    = pe("5332455502917949981", "🏦")
E_DOWN    = pe("5305522282695768654", "👇")
E_RIGHT   = pe("6185707729009512236", "👉")
E_WAVE    = pe("5413694143601842851", "👋")
E_CROWN   = pe("5217822164362739968", "👑")
E_SHIRT   = "👕"   # no custom id provided
E_GROUP   = pe("5453957997418004470", "👥")
E_DIAMOND = pe("5427168083074628963", "💎")
E_BLUE    = pe("6104780447684757396", "💙")
E_MUSCLE  = pe("5388755604976186348", "💪")
E_CHAT    = pe("5443038326535759644", "💬")
E_MONEY   = pe("5224257782013769471", "💰")
E_CASH    = pe("5409048419211682843", "💵")
E_PLANE   = pe("5201691993775818138", "✈️")
E_BAG     = pe("5445221832074483553", "💼")
E_CAL     = pe("5413879192267805083", "📅")
E_CHART   = pe("5244837092042750681", "📈")
E_BAR     = pe("5231200819986047254", "📊")
E_BOOK    = pe("5222444124698853913", "🔖")
E_PHONE   = pe("5330237710655306682", "📱")
E_VIDEO   = "📹"   # no custom id provided
E_TV      = pe("5355012477883004708", "📺")
E_SEARCH  = pe("5231012545799666522", "🔍")
E_KEY     = pe("5278573677900752088", "🔑")
E_FIRE    = pe("5424972470023104089", "🔥")
E_SMILE   = pe("5386587088873331829", "😄")
E_HAPPY   = pe("5461117441612462242", "🙂")
E_COOL    = pe("5368562433981947135", "😎")
E_SAD     = pe("5303029131489850425", "😔")
E_PRAY    = pe("5231249426130935149", "🙏")
E_ROCKET  = pe("5188481279963715781", "🚀")
E_CAR     = pe("5233638613358486264", "🚗")
E_RCAR    = pe("5253752975997803460", "🚘")
E_STOP    = pe("5413610645142642221", "🛑")
E_SHIELD  = pe("5197288647275071607", "🛡️")
E_ROBOT   = pe("5465277190453085073", "🤖")
E_LION    = pe("5316961893728926221", "🦁")
E_BRAIN   = pe("5226639745106330551", "🧠")
E_FAM     = "👨‍👩‍👧"  # no custom id provided
E_STAR2   = pe("5267500801240092311", "⭐")

# ── KEY LINKS (defined at top of file) ────────────────────────────────────────

# ── SUPPORT LINE (added to every reply) ───────────────────────────────────────
SUPPORT_LINE = f'\n\n{E_CHAT} <b>24/7 Live Human Chat Support — Message {SUPPORT_USER}</b>'

def bold(text):
    """Wrap text in HTML bold and add support line"""
    return f"<b>{text}</b>{SUPPORT_LINE}"

def detect_language(text: str) -> str:
    t = text.lower()
    hindi_words = ["kya","hai","hain","bhai","karo","kaise","nahi","toh","aur","mein","se","ke","ko","ka","ki","na","ab","jo","yeh","woh","agar","tum","main","aaj","kal","mat","bolo","batao","chahiye","lagta","milta","hota","hoga","karta","karti","milega","padega","chahte","jaana","karna","lena","dena","suno","dekho","poochho","samjho","sab","sirf","bahut","zyada","thoda","accha","theek","sahi","galat","pehle","baad","abhi","jaldi","hamesha","kabhi","phir","wapas"]
    english_words = ["what","how","when","where","why","who","is","are","can","will","do","does","the","and","or","but","for","with","from","about","want","need","tell","know","please","help","join","get","make","use","have","would","could","should","any","all","more","much","very","also","just","only","even","still","always","never","again","here","there","now","then","yes","no","okay","sure","thanks","hello","bye","good","great","nice","best","better","free","paid","money","profit","loss","trade","trading","signal","vip","account","deposit","withdraw","really","actually","basically"]
    hindi_count = sum(1 for w in hindi_words if w in t.split())
    english_count = sum(1 for w in english_words if w in t.split())
    if english_count > hindi_count and hindi_count == 0:
        return "english"
    return "hinglish"

def smart_reply(text: str) -> str:
    t = text.lower().strip()
    lang = detect_language(text)
    # Whole-word set so short triggers ("ok","hey","ty") can't fire inside
    # longer words like "brOKer", "tHEY", "qualiTY".
    _words = set(t.replace("!", " ").replace("?", " ").replace(",", " ").replace(".", " ").split())

    # ── TELEGRAM/FRAUD (before scam block) ────────────────────────────────────
    if any(w in t for w in ["telegram pe sach fraud","telegram mein bahut fraud","telegram fraud hota","telegram safe hai kya","fraud on telegram","telegram par dhoka","telegram fraud bahut","telegram pe dhoka hota"]):
        return bold(f"{E_WARN} Haan bhai, Telegram par bahut fraud log bhi baithe hain!\n\nIsliye join karne se pehle:\n{E_CHECK} History check karo\n{E_CHECK} Proofs dekho\n{E_CHECK} Testimonials check karo\n{E_CHECK} Reputation verify karo\n\nKabhi jaldi decision mat lo!\n\nthe team ka sab kuch publicly verifiable hai! {E_CHECK}\n\n{E_TV} Public Channel: {TG_CHANNEL}")

    if any(w in t for w in ["93 96 accuracy real","accuracy real ya fake","accuracy fake ya real","93 accuracy fake","96 accuracy real","accuracy genuine kya","win rate real","accuracy sach","accuracy verified kya","real"]):
        return bold(f"{E_TARGET} Accuracy — Real hai!\n\nYeh REAL performance par based hai!\n\n{E_CHECK} Quotex ke official page par verified\n{E_CHECK} the team official Trader of the Week hain\n{E_CHECK} Public channel pe daily result proofs\n{E_CHECK} Live trading recordings available\n\n{E_TV} Khud check karo: {TG_CHANNEL}")

    # ── SCAM/TRUST ─────────────────────────────────────────────────────────────
    if any(w in t for w in ["scam","fraud","fake","real nahi","jhooth","dhoka","trust nahi","believe nahi","fake trader","scammer","cheat","cheating","genuine nahi","sach nahi","bewakoof"]):
        return bold(f"{E_LION} Yeh scam toh nahi? Sach bolunga!\n\nHum kisi bhi cheez ka pressure nahi dete — join karne se pehle khud verify karo:\n{E_RIGHT} Trading Wolf Public Channel dekho\n{E_RIGHT} Members ke feedback padho\n{E_RIGHT} Apna research karo\n\nDecision 100% tumhara hai! Koi pressure nahi! {E_PRAY}")

    # ── SUPPORT ────────────────────────────────────────────────────────────────
    if any(w in t for w in ["tradelikenoah","trade like noah","24/7 support","support team","contact support","help center","helpline","customer care","kaise contact","sampark kaise","phone number","contact number","bhai help karo","help chahiye","madad chahiye","instagram kya","social media","facebook","whatsapp number","kaise baat kare"]):
        return bold(f"{E_CHAT} Support chahiye? Direct contact karo!\n\n{E_RIGHT} {SUPPORT_USER}\n\nTeam 24/7 available hai! {E_CHECK}\n\nKoi bhi problem — deposit, withdrawal, signals, registration — sab help milegi! {E_FIRE}")

    if any(w in t for w in ["support team alag","alag support team","vip support alag","same support","equal support milti","support mein fark","vip non vip support"]):
        return bold(f"{E_CHECK} Nahi bhai! Mere liye har member equal hai!\n\nChahe VIP ho ya non-VIP — sabko same support milti hai!\n\nKoi discrimination nahi! {E_MUSCLE}")

    # ── SPECIFIC BLOCKS ────────────────────────────────────────────────────────
    if any(w in t for w in ["paise withdraw hote hain kya","sirf deposit hota","withdraw bhi hota","paise nikalta hai kya","withdrawal bhi hota"]):
        return bold(f"{E_MONEY} Dono hote hain bhai!\n\nDeposit bhi hota hai aur Withdrawal bhi!\n\nHum apni team ke through kaafi deposits aur withdrawals handle kar chuke hain!\n\nWithdrawal process:\n1{E_RIGHT} Quotex pe Withdraw section\n2{E_RIGHT} Amount daalo\n3{E_RIGHT} Method choose karo\n4{E_RIGHT} Done! {E_BOLT}\n\nVIP members ka koi withdrawal problem nahi! {E_FIRE}")

    if any(w in t for w in ["quotex withdrawal block kar","block withdrawal","quotex ne block","withdrawal rok diya quotex","payment block quotex"]):
        return bold(f"{E_CHECK} Bina wajah koi company withdrawals block nahi karti!\n\nAgar:\n{E_CHECK} KYC complete hai\n{E_CHECK} Account genuine hai\n{E_CHECK} Rules follow ho rahe hain\n\nToh withdrawal smooth hoti hai!\n\nthe team ko personally koi issue nahi hua! {E_MUSCLE}")

    if any(w in t for w in ["kyc ke baad withdrawal kab","kyc complete ke baad nikalna","kyc hone ke baad paise","kyc ho gaya withdrawal kab","after kyc paise kab"]):
        return bold(f"{E_CHECK} KYC complete hone ke baad withdrawal smoothly process hota hai!\n\nthe team ko personally koi KYC withdrawal issue nahi hua!\n\nKYC jaldi complete karo — smooth experience milega! {E_MUSCLE}")

    if any(w in t for w in ["pehle loss hua kisi se","pehle kisi ne loss diya","doosre se loss hua","pehle cheated","trust issue hai","trust karna mushkil"]):
        return bold(f"{E_BLUE} Samajh sakta hoon bhai!\n\nTrust karne se pehle khud verify karo:\n{E_CHECK} Trading Wolf Public Channel dekho\n{E_CHECK} YouTube: {YOUTUBE}\n{E_CHECK} Quotex pe Trader of the Week status verify karo\n{E_CHECK} Members ke testimonials padho\n\nKoi pressure nahi — decision 100% tumhara hai! {E_PRAY}")

    if any(w in t for w in ["revenge trading kya hota","revenge trade meaning","revenge trading define","revenge kya hai trading","revenge trade kya","what is revenge trade"]):
        return bold(f"{E_WARN} Revenge Trading kya hoti hai?\n\nJab trader loss ke baad emotional hoke badi amounts se trade karta hai loss recover karne ke liye — use Revenge Trading kehte hain!\n\nYeh bahut dangerous hai!\n\nRule: Loss hua toh us din logout karo aur kal fresh start karo! {E_MUSCLE}\n\nthe team VIP mein yeh psychology sikhate hain! {E_FIRE}")

    if any(w in t for w in ["full time trading career","trading full time","trading ko career","trading job ban sakta","trading se full time income","full time trading possible"]):
        if lang == "english":
            return bold(f"{E_BAG} Can trading be a full-time career?\n\nABSOLUTELY YES! {E_CHECK}\n\nthe team has been full-time trading for years!\n\nBut remember:\n{E_WARN} Don't quit job immediately\n{E_WARN} First learn properly\n{E_WARN} Build consistent profits for 3-6 months\n{E_WARN} Then consider full-time\n\nTrading = Financial freedom! {E_MONEY}")
        return bold(f"{E_BAG} Trading full-time career ban sakta hai?\n\nABSOLUTELY YES! BILKUL BAN SAKTA HAI! {E_CHECK}\n\nthe team kai saalon se full-time trading kar rahe hain!\n\nLekin yaad rakho:\n{E_WARN} Abhi job mat chhodo\n{E_WARN} Pehle properly seekho\n{E_WARN} 3-6 mahine consistent profit banao\n{E_WARN} Phir full-time consider karo\n\nTrading = Financial freedom! {E_MONEY}")

    if any(w in t for w in ["live account proof dikhate","apna live account dikha","own account dikhao","personal live account proof","live account screenshot dikha"]):
        return bold(f"{E_CHECK} Haan! the team apne live account ke proofs bhi share karte hain!\n\nSab transparent hai!\n\n{E_TV} Public Channel pe dekh sakte ho:\n{TG_CHANNEL}")

    if any(w in t for w in ["real feedback kahan","actual feedback","genuine feedback kahan","log kya kehte hain","members ke reviews","actual reviews","member opinions"]):
        return bold(f"{E_CHAT} Real feedback available hai bhai!\n\nPublic Channel par:\n{E_CHECK} Real member videos\n{E_CHECK} Profit screenshots\n{E_CHECK} Withdrawal proofs\n{E_CHECK} Genuine reviews\n\n{E_TV} Check karo: {TG_CHANNEL}")

    if any(w in t for w in ["losses bhi openly","openly losses share","loss bhi share karte","loss dikhate ho bhi","loss results bhi","loss bhi post karte"]):
        return bold(f"{E_MUSCLE} Haan bhai! the team losses bhi openly share karte hain!\n\nTrading mein profit aur loss dono part hote hain!\n\nthe team transparency maintain karte hain — yahi unhe genuine banata hai! {E_CHECK}")

    if any(w in t for w in ["zero se start successful","zero se karke","zero knowledge se","kuch nahi tha phir bhi","zero se profitable","nothing se start"]):
        return bold(f"{E_TROPHY} Zero Se Successful!\n\nHaan bhai! Kai members zero se start karke profitable bane hain!\n\nHar kisi ka result alag hota hai, guarantee nahi — lekin discipline aur consistency se progress possible hai! {E_FIRE}\n\nAgar woh kar sakte hain — tum bhi kar sakte ho! {E_MUSCLE}")

    if any(w in t for w in ["shuru mein kya dhyan","beginning mein tips","starting tips","shuru karne ki tips","start karte waqt","trading shuru karne se pehle dhyan"]):
        return bold(f"{E_STAR} Shuru mein yeh dhyan rakho bhai!\n\n1{E_RIGHT} Patience rakho — jaldi paise ki soch galat hai\n2{E_RIGHT} Money management follow karo\n3{E_RIGHT} Risk management sikho\n4{E_RIGHT} Demo se practice karo\n5{E_RIGHT} Emotional hoke trade mat karo\n\nYeh basics strong hone ke baad hi live trade karo! {E_MUSCLE}")

    if any(w in t for w in ["beginner experienced alag","naye aur experienced alag","different level guidance","level wise milti","experienced ko alag","beginner vs experienced guidance"]):
        return bold(f"{E_TARGET} Haan bhai! Guidance level-wise di jaati hai!\n\nBeginners ke liye:\n{E_BOOK} Money management\n{E_BOOK} Basic signals\n{E_BOOK} Risk management\n\nExperienced ke liye:\n{E_BAR} Advanced strategies\n{E_BAR} Complex setups\n{E_BAR} Market analysis\n\nHar member ke level ke hisaab se! {E_DIAMOND}")

    if any(w in t for w in ["maximum signals lose","kitne signals fail","ek din mein loss signals","losing signals ek din","signals loss count","signal failure rate"]):
        return bold(f"{E_BAR} Ek din mein maximum 1-2 signals hi loss hue hain!\n\nAgar 10 signals diye toh 9-10 winning side mein hote hain!\n\nAccuracy varies session to session and is never guaranteed. {E_FIRE}\n\nKabhi kabhi 99% bhi! {E_DIAMOND}")

    if any(w in t for w in ["late entry lena","late mein le saku","signal miss late entry","after miss late entry","late entry possible"]):
        return bold(f"{E_CROSS} 100% recommend karunga ki late entry bilkul mat lo bhai!\n\nChhoti si candle movement bhi tumhe loss mein daal sakti hai!\n\nNext signal ka wait karo — aur aate hain! {E_FIRE}\n\nHar 1-2 ghante mein naye opportunities milte hain VIP mein! {E_CHECK}")

    if any(w in t for w in ["vip group kitne log","vip mein total log","vip group total members","vip members total","vip group size total","vip kitne sadsya"]):
        return bold(f"{E_GROUP} VIP group mein an active community hain!\n\nAur public Telegram community mein a growing Telegram community!\n\nYouTube pe a growing YouTube audience!\n\nEk strong aur growing family hai! {E_MUSCLE}")

    if any(w in t for w in ["quotex alternative","binomo kya","other trading app","dusra trading app","quotex chhod ke","quotex ki jagah koi","alternative broker kya","binomo use karu"]):
        return bold(f"{E_DIAMOND} the team personally Quotex recommend karte hain!\n\nLekin agar alternative chahiye toh Binomo bhi dekh sakte ho!\n\nWolf ke signals mainly Quotex ke liye optimized hain!\n\nBest experience Quotex pe hi milega! {E_FIRE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    if any(w in t for w in ["naye traders ko advice","new traders advice","traders ko kya bologe","newcomers advice dena","advice for new","naye ke liye advice"]):
        return bold(f"{E_CHAT} New Traders ke liye the team ki advice!\n\nKabhi haar mat maano, losses se daro mat, aur discipline ke saath seekhte raho.\n\n— the Wolf team {E_FIRE}\n\nAur yeh bhi:\n{E_CHECK} Demo se start karo\n{E_CHECK} Money management seekho\n{E_CHECK} Jaldi paise ki soch mat rakhna\n{E_CHECK} Consistency maintain karo! {E_MUSCLE}")

    # ── SIGNALS TODAY / GENERAL SIGNAL QUESTIONS ──────────────────────────────
    if any(w in t for w in ["aaj signal","signal aayega","signal doge","signal aaj","aaj koi signal","signal milega aaj","signal hai kya","aaj trading","signal abhi","koi signal","daily signals kitne","number of signals","kitne signals milte","daily mein kitne signal","ek din mein kitne","signals per day","signal count"]):
        return bold(f"{E_TARGET} Aaj ke signals! Har din 10-20 signals VIP mein aate hain!\n\n5 sessions daily:\n{E_SUN} Morning: 8 AM – 10 AM\n{E_CLOUD} Afternoon: 12 PM – 2 PM\n{E_SUNSET} Evening: 4 PM – 6 PM\n{E_MOON} Night: 8 PM – 10 PM\n{E_NIGHT} Late Night: 10 PM – 1 AM\n\nVIP join karo aur aaj se 10-20 signals pao! {E_FIRE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    # ── WHO AM I / BOT INFO ────────────────────────────────────────────────────
    if any(w in t for w in ["tum kaun ho","aap kaun ho","kya bot hai","kya ye bot","who are you","who r u","bot hai kya","kya aap bot","tum kya ho","kaun hain aap"]):
        return bold(f"{E_ROBOT} Main Trading Wolf Bot hoon!\n\nMain the Wolf team ki taraf se hun — India ke top binary trader!\n\nMain tumhari help kar sakta hoon:\n{E_CHART} Trading ke baare mein\n{E_CROWN} VIP join karne mein\n{E_MONEY} Deposit aur bonus\n{E_LION} the team ke baare mein\n\nKya jaanna chahte ho? {E_FIRE}")

    # ── SOCIAL MEDIA / CONTACT ─────────────────────────────────────────────────
    if any(w in t for w in ["instagram","insta","facebook","whatsapp","social media","contact number","phone number","mobile number","kaise baat kare","direct contact"]):
        return bold(f"{E_PHONE} Trading Wolf ke social media:\n\n{E_TV} YouTube: youtube.com/@trading_withwolf\n{E_CHAT} Telegram: {SUPPORT_USER}\n{E_RIGHT} Public Channel: {TG_CHANNEL}\n\nSabse best: Telegram pe message karo!\n\n{E_RIGHT} Direct contact: {SUPPORT_USER} {E_FIRE}")

    # ── APP DOWNLOAD ───────────────────────────────────────────────────────────
    if any(w in t for w in ["quotex download","app download","app install","quotex app download","platform install","trading app download","kaise download"]):
        return bold(f"{E_PHONE} Quotex App Download kaise kare?\n\n{E_RIGHT} Android: Play Store mein 'Quotex' search karo\n{E_RIGHT} iOS: App Store mein 'Quotex' search karo\n{E_RIGHT} Web: quotex.io pe jaao\n\nApp install karne ke baad Wolf ke link se register karo:\n{E_RIGHT} {REGISTER_LINK}\n\nCode WOLF50 use karo — 50% bonus! {E_GIFT}")

    # ── DOUBT / CONFUSION ──────────────────────────────────────────────────────
    if any(w in t for w in ["doubt hai","koi doubt","confusion hai","samajh nahi","kuch samajh","samajh nahi aaya","confuse hoon","clear nahi","sahi bata","sach bata","kya sahi hai"]):
        return bold(f"{E_CHAT} Koi bhi doubt ho — poochho bhai! {E_HAPPY}\n\nMain yahan hoon help ke liye!\n\nYa directly contact karo:\n{E_RIGHT} {SUPPORT_USER}\n\nCommon doubts:\n{E_CHECK} VIP free hai? — Haan! Sirf ${MIN_DEPOSIT} deposit\n{E_CHECK} Signals accurate? — well-reviewed\n{E_CHECK} Withdrawal hoga? — Bilkul!\n{E_CHECK} Experience chahiye? — Bilkul nahi!\n\nAur kya jaanna chahte ho? {E_FIRE}")

    # ── SHOULD I JOIN ──────────────────────────────────────────────────────────
    if any(w in t for w in ["join karna chahiye","mujhe join karna","mere liye sahi","kya join karu","joining sahi hai","join karna sahi","suggest karo","kya karu main","kya karoon","is it worth","worth it"]):
        return bold(f"{E_LION} Bhai main honestly batata hoon!\n\nAgar tum:\n{E_CHECK} Trading se extra income chahte ho\n{E_CHECK} Trading seekhna chahte ho\n{E_CHECK} Seriously disciplined ho\n\nToh VIP ZAROOR join karo! {E_FIRE}\n\nAgar tum:\n{E_CROSS} Jaldi ameer hona chahte ho\n{E_CROSS} Risk bilkul nahi lena chahte\n\nToh pehle basics seekho!\n\nFir decide karo! {E_PRAY}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    # ── EXPERIENCE NEEDED ──────────────────────────────────────────────────────
    if any(w in t for w in ["experience chahiye","experience nahi","naye logon ke liye","experience nahi hai","pehle kabhi nahi","koi experience","trading experience","experience lagta"]):
        return bold(f"{E_CHECK} Bilkul koi experience nahi chahiye bhai!\n\nWolf ke signals follow karna bahut simple hai:\n{E_RIGHT} Signal aata hai → Trade lagao → Profit!\n\nWolf khud explain karte hain:\n{E_CHECK} Pehle kya karna hai\n{E_CHECK} Kaise trade lagana hai\n{E_CHECK} Risk kaise manage kare\n\nBeginners bhi easily follow kar sakte hain! {E_FIRE}\n\n{E_RIGHT} Demo se start karo: {REGISTER_LINK}")

    # ── FREE SIGNALS ───────────────────────────────────────────────────────────
    if any(w in t for w in ["free signal","free mein signal","free signals chahiye","free wala signal","bina paisa signal","kya free signal"]):
        return bold(f"{E_GIFT} Free signals ke liye Public Channel join karo!\n\n{E_RIGHT} {TG_CHANNEL}\n\nPublic channel mein:\n{E_CHECK} Kuch free signals\n{E_CHECK} Daily results\n{E_CHECK} Trading tips\n\nLekin FULL VIP signals ke liye:\n{E_CHECK} 10-20 daily signals\n{E_CHECK} a strong track record\n{E_CHECK} Personal guidance\n\nVIP join karo sirf ${MIN_DEPOSIT} mein! {E_FIRE}\n\n{E_RIGHT} {REGISTER_LINK}")

    # ── MINIMUM AMOUNT ─────────────────────────────────────────────────────────
    if any(w in t for w in ["minimum amount","kam se kam kitna","least amount","kitna se start","minimum kitna","minimum se","starting minimum"]):
        return bold(f"{E_MONEY} Minimum deposit: ${MIN_DEPOSIT}\n\nPro tip:\n{E_GIFT} Code  use karo\n{E_CHECK} 50% bonus milega WOLF50 code ke saath!\n\nRecommended: $100 (Rs.8,000)\n{E_CHART} Better growth possible\n{E_SHIELD} Proper risk management\n\n{E_RIGHT} Register karo: {REGISTER_LINK}")

    # ── LOSS RECOVERY ──────────────────────────────────────────────────────────
    if any(w in t for w in ["loss recover karna","loss recovery chahiye","loss wapas","ghata recover","paise wapas chahiye","loss cover","recovery chahiye"]):
        return bold(f"{E_MUSCLE} Loss recover karna hai? VIP join karo!\n\nthe team ke signals se:\n{E_CHECK} a strong track record\n{E_CHECK} Systematic compounding\n{E_CHECK} Proper risk management\n\nExample:\nRs.5,000 loss → VIP join karo → Signals follow karo → 1-2 months mein recover possible!\n\nHazaaron members ne loss recover kiya hai! {E_FIRE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    # ── HOW MUCH CAN I EARN ────────────────────────────────────────────────────  
    if any(w in t for w in ["kitna kamaoonga","kitna milega","kitna earn","kitna profit","how much earn","daily kitna","income kitni","how much can i make"]):
        return bold(f"{E_MONEY} Kitna kama sakte ho?\n\nDepends on your capital:\n{E_CASH} Results vary depending on capital and are never guaranteed\n\nPehle mahine mein Results vary a lot from person to person and are never guaranteed. {E_CHART}\n\nCondition:\n{E_CHECK} Signals follow karo\n{E_CHECK} Risk management karo\n{E_CHECK} Emotional mat ho\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    # ── PAYMENT METHOD ─────────────────────────────────────────────────────────
    if any(w in t for w in ["payment kaise kare","payment method","kaise payment","payment options","payment mode","kitne payment","payment karte kaise"]):
        return bold(f"{E_MONEY} Payment methods available hain:\n\n{E_CHECK} UPI (Google Pay, PhonePe, Paytm)\n{E_CHECK} Net Banking\n{E_CHECK} Cryptocurrency\n{E_CHECK} Trust Wallet\n{E_CHECK} Bank Transfer\n\nSabse easy: UPI se direct deposit!\n\nCode WOLF50 use karo → 50% bonus! {E_GIFT}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    # ── HOW LONG / TIME QUERIES ────────────────────────────────────────────────
    if any(w in t for w in ["kitna time lagta","kitne din mein","kab tak milega","kab milega","kab tak","time kitna lagta","kitne time","jaldi chahiye","turant chahiye"]):
        return bold(f"{E_CLOCK} Time queries ka jawab!\n\n{E_RIGHT} VIP access: Same day! {E_BOLT}\n{E_RIGHT} Signals: Same day se shuru!\n{E_RIGHT} Withdrawal: 10 min (crypto) / 24-48 hrs (UPI)\n{E_RIGHT} Profit: First trade se hi!\n\nBahut fast process hai bhai! {E_FIRE}\n\n{E_RIGHT} Register karo: {REGISTER_LINK}")

    # ── RISK ───────────────────────────────────────────────────────────────────
    if any(w in t for w in ["risk kitna hai","kitna risk","risk hai kya","risk of trading","trading mein risk","risk hoga","risky hai","safe hai trading"]):
        return bold(f"{E_SHIELD} Trading mein risk ke baare mein!\n\nHaan, trading mein risk hota hai!\n\nLekin Wolf ke saath risk manage hota hai:\n{E_CHECK} 1% per trade rule\n{E_CHECK} Stop-loss guidance\n{E_CHECK} Non-Martingale approach\n{E_CHECK} Daily risk limit\n\nSirf woh invest karo jo afford karo!\n\nPehle demo pe practice karo! {E_GAME}\n\n{E_RIGHT} Register: {REGISTER_LINK}")



    # ── GREETINGS ──────────────────────────────────────────────────────────────
    if any(w in _words for w in ["hello","hii","hey","helo","wassup","hlo","hellow","hye","heya","hi"]):
        if lang == "english":
            return bold(f"{E_WAVE} Hello! I'm Trading Wolf Bot! {E_ROBOT}\n\nAsk me about:\n{E_CHART} What is trading\n{E_MONEY} How to join VIP\n{E_GIFT} Bonus code\n{E_LION} About the team (Trading Wolf)\n{E_TV} YouTube channel\n{E_CLOCK} Signal timings\n\nWhat would you like to know? {E_FIRE}")
        return bold(f"{E_WAVE} Hello bhai! Main hoon Trading Wolf Bot! {E_ROBOT}\n\nMujhse pooch sakte ho:\n{E_CHART} Trading kya hai\n{E_MONEY} VIP kaise join kare\n{E_GIFT} Bonus code\n{E_LION} the team (Trading Wolf) ke baare mein\n{E_TV} Channel link\n{E_CLOCK} Signal timings\n\nKya jaanna chahte ho? {E_FIRE}")

    if any(w in t for w in ["namaste","namaskar","jai hind","pranam"]):
        return bold(f"{E_PRAY} Namaste bhai! Swagat hai Trading Wolf Bot mein!\n\nYahan se tum:\n{E_CHECK} Trading seekh sakte ho\n{E_CHECK} VIP signals pa sakte ho\n{E_CHECK} Daily profit kama sakte ho\n\nKya help chahiye? {E_ROCKET}")

    if any(w in t for w in ["good morning","gm ","subah ko","morning bhai"]):
        if lang == "english":
            return bold(f"{E_SUN} Good morning! Today is a great day for trading!\n\nVIP members' signals are ready {E_CHART}\n\nJoin now and start earning! {E_FIRE}")
        return bold(f"{E_SUN} Good morning bhai! Aaj ka din trading ke liye perfect hai!\n\nVIP members ke liye signals ready hain {E_CHART}\n\nAbhi join karo! {E_FIRE}")

    if any(w in t for w in ["good night","gn bhai","raat ko","good night bhai"]):
        return bold(f"{E_MOON} Good night bhai! Kal fresh mind se trading karna!\n\nVIP join karo aur kal se signals pao {E_DIAMOND}\n\nSweet dreams! {E_HAPPY}")

    if any(w in t for w in ["good evening","shaam ko","evening bhai"]):
        return bold(f"{E_SUNSET} Good evening bhai! Evening session ke signals VIP mein live hain! {E_FIRE}\n\nAbhi join karo! {E_CHART}")

    if any(w in t for w in ["how are you","kaise ho bhai","kaisa hai bhai","kya haal bhai","sab theek","kya chal raha"]):
        if lang == "english":
            return bold(f"{E_COOL} I'm doing great! How about you? Making money from trading?\n\nIf not, join VIP now!\n{E_FIRE} Daily 10-20 signals\n{E_MONEY} a strong track record\n\nWhat can I help you with? {E_HAPPY}")
        return bold(f"{E_COOL} Main ekdum mast hoon bhai! Aur tum? Trading se paise kama rahe ho?\n\nNahi? Toh abhi VIP join karo!\n{E_FIRE} Daily 10-20 signals\n{E_MONEY} a strong track record\n\nKya poochna hai? {E_HAPPY}")

    # ── ABOUT THE TEAM ─────────────────────────────────────────────────────────────
    if any(w in t for w in ["noah kaun","who is noah","noah kya","about noah","akshay kaun","akshay pandit","trading noah kaun","noah ke baare","noah story","noah ki kahani","milkman","doodh bechta","founder","akshay ji","noah bhai kaun","story batao","akshay ki story","noah ki story","trading noah story","inke baare mein","iske baare mein"]):
        if lang == "english":
            return bold(f"{E_LION} About the Wolf team\n\nKnown as: Trading Wolf\n\nWe started this community to teach trading properly — the basics, risk management, and discipline — and to give members a place to get quality signals and support.\n\nLike any trader, we've had wins and losses along the way. What matters is sticking with a disciplined, honest approach.\n\nMission: Teach trading properly to as many people as possible! {E_FIRE}")
        return bold(f"{E_LION} Trading Wolf ke baare mein\n\nHum yeh community isliye bana rahe hain taaki log trading properly seekh sakein — basics, risk management, aur discipline ke saath.\n\nHar trader ki tarah humne bhi wins aur losses dekhe hain. Jo matter karta hai woh hai disciplined aur honest approach maintain karna.\n\nMission: Jitna ho sake utne logon ko sahi tareeke se trading sikhana! {E_FIRE}")

    if any(w in t for w in ["noah naam","trading noah naam","naam kaise mila","brand name","noah name idea","naam kisne diya"]):
        return bold(f"{E_STAR} Trading Wolf naam ek strong, disciplined trading community banane ke socha gaya tha! {E_HAPPY}\n\nAur aaj yeh dheere dheere grow kar raha hai! {E_TROPHY}")

    if any(w in t for w in ["motivation","inspire","motivate","kya motivate","himmat","hausla","strength kahan"]):
        return bold(f"{E_MUSCLE} the team ki sabse badi motivation:\n\nApne maa-baap ko khush aur achhi zindagi jeete hue dekhna!\n\nPehli car kharidi aur maa-baap proud the — woh moment priceless tha! {E_RCAR}{E_BLUE}\n\nJitni jaldi mehnat karo utni jaldi zindagi aaraam se guzregi! — the team {E_FIRE}")

    if any(w in t for w in ["favourite quote","best quote","life quote","trading quote","apka quote","quote batao","famous quote"]):
        return bold(f"{E_CHAT} the team ka favourite quote:\n\nZindagi mein kabhi rukna mat. Jitni jaldi mehnat karke successful banoge, utni hi aage ki zindagi aaraam se guzregi.\n\n— the Wolf team {E_FIRE}")

    if any(w in t for w in ["achievement","biggest achievement","dream home","ghar kharida","mustang","xuv700","scorpio","gaadi","crore","property","car kharidi","vehicle"]):
        return bold(f"{E_TROPHY} the team ke baare mein:\n\n{E_TROPHY} Trading ko full-time approach ki tarah leते hain\n{E_GROUP} Ek growing community build ki hai\n{E_CHECK} Members ke saath transparent rehte hain\n\nFocus hamesha rehta hai: sahi tareeke se trading sikhana! {E_FIRE}")

    if any(w in t for w in ["trading kab start","2020","kab se trade","trading journey kab","journey start","trading shuru kab"]):
        return bold(f"{E_CAL} the team ne 2020 mein trading start ki thi!\n\nAur 3 saal ki mehnat ke baad 2023 tak consistent profitable trader ban gaye! {E_MUSCLE}\n\nAaj woh India ke top binary traders mein se ek hain! {E_TROPHY}")

    if any(w in t for w in ["pehla profit","first profit","pehli kamai","pehla paisa","first earning","5000"]):
        return bold(f"{E_MONEY} the team ne bhi ek chhoti amount se hi trading start ki thi!\n\nDheere dheere seekhte hue confidence build hui! {E_PARTY}\n\nHar trader ki journey chhoti shuruaat se hi hoti hai! {E_FIRE}")

    if any(w in t for w in ["sabse bada loss","biggest loss","25 lakh","loss 2025","loss 2026","nuksaan kitna","maximum loss"]):
        return bold(f"{E_SAD} Trading mein losses sabko hote hain — the team ko bhi hue hain!\n\nLekin haar maanne ke bajaye, risk management par focus karke dheere dheere recover kiya! {E_MUSCLE}\n\nDiscipline hi asli fark banata hai! {E_FIRE}{E_TROPHY}")

    if any(w in t for w in ["wapas kaise aaye","loss recovery","loss ke baad","comeback","recover kaise","loss se wapas"]):
        return bold(f"{E_MUSCLE} Loss ke baad wapas kaise aaye the team?\n\n{E_CHECK} Experience aur knowledge ka use kiya\n{E_CHECK} Chhoti amounts se recovery start ki\n{E_CHECK} Risk management par kaam kiya\n{E_CHECK} Dheere dheere khud ko build kiya\n{E_CHECK} Kabhi haar nahi maani!\n\nAaj Trader of the Week aur India ke top traders mein! {E_TROPHY}")

    if any(w in t for w in ["trader of the week","quotex award","official award","quotex recognition","top trader","india top trader"]):
        return bold(f"{E_TROPHY} Haan bhai!\n\nthe team officially Quotex ke Trader of the Week reh chuke hain!\n\nYeh ek bada recognition hai jo sirf top performing traders ko milta hai! {E_FIRE}\n\nQuotex ke official pages par verify kar sakte ho! {E_CHECK}")

    if any(w in t for w in ["clothing brand","kapde","brand","akshay ka business","trading ke alawa"]):
        return bold(f"{E_SHIRT} the team ka trading ke alawa ek clothing brand bhi hai!\n\nSaath hi family ke saath time spend karte hain aur life enjoy karte hain! {E_HAPPY}\n\nTrading ne unhe financial freedom di hai! {E_FIRE}")

    if any(w in t for w in ["family support","family ne","ghar wale","parents","maa baap","wife","family trading"]):
        return bold(f"{E_FAM} Bahut logon ki family shuru mein trading ko support nahi karti! {E_SAD}\n\nLekin discipline aur consistent results dekh kar dheere dheere trust build hota hai! {E_BLUE}\n\nTumhare apno ka trust bhi waise hi banega — time aur consistency ke saath! {E_FIRE}")

    if any(w in t for w in ["future plan","aage kya","goal kya","akshay ka goal","expand","international","app launch","trading noah app"]):
        return bold(f"{E_ROCKET} the team ka future plan:\n\nGoal: Zyada se zyada logon ko FREE mein trading sikhana!\n\nInternational level par Trading Wolf community expand karna!\n\nPossible future: Trading Wolf App (abhi final plan nahi)\n\nUnka mission jaari hai! {E_FIRE}")

    if any(w in t for w in ["offline meetup","event","seminar","milna","meet karna","offline event","meetup"]):
        return bold(f"{E_HAPPY} Abhi tak the team offline meetups organise nahi karte!\n\nLekin future mein ho sakta hai!\n\nAbhi ke liye:\n{E_RIGHT} YouTube: {YOUTUBE}\n{E_RIGHT} Telegram: {SUPPORT_USER}\n{E_RIGHT} Channel: {TG_CHANNEL}")

    if any(w in t for w in ["trading book","course launch","paid course","book likhenge","training videos"]):
        return bold(f"{E_BOOK} the team already educational content aur training videos provide kar chuke hain!\n\nAur sabse best baat — yeh FREE mein milta hai!\n\n{E_TV} YouTube Course (Basic to Advanced):\n{COURSE_LINK}\n\nVIP mein aur bhi advanced education milti hai! {E_DIAMOND}")

    if any(w in t for w in ["ek sentence advice","one advice","best advice","single advice","ek tip","ek baat"]):
        return bold(f"{E_CHAT} the team ki sabse best advice:\n\nKabhi haar mat maano, losses se daro mat, aur discipline ke saath seekhte raho.\n\n— the Wolf team {E_FIRE}")

    if any(w in t for w in ["trading nahi hota","alternative","kya karte","backup plan","job karte","trading na hoti toh"]):
        return bold(f"{E_BRAIN} the team ka maanna hai ki har insaan ke paas ek backup plan hona chahiye — jaise job ya business! {E_BAG}\n\nLekin trading ne unki zindagi badal di! {E_FIRE}\n\nAur woh chahte hain ki aap bhi yeh change experience karo! {E_MUSCLE}")

    if any(w in t for w in ["ghante trade","kitne ghante","trading hours","daily kitna","how many hours","work hours"]):
        return bold(f"{E_CLOCK} the team average 4-5 ghante actively trading karte hain!\n\nBaaki time:\n{E_BAR} Market analysis\n{E_FAM} Family time\n{E_TARGET} Community management\n\nTrading ne unhe time freedom bhi di hai! {E_FIRE}")

    if any(w in t for w in ["emotional moment","sabse emotional","proud moment","car moment","parents proud","emotional story"]):
        return bold(f"{E_RCAR} the team ka sabse emotional moment:\n\nJab unhone pehli car kharidi aur apne maa-baap ko khush aur proud dekha!\n\nWoh moment unke liye priceless tha! {E_BLUE}\n\nYeh moment unhe har din mehnat karne ki inspiration deta hai! {E_FIRE}")

    # ── TRADING ────────────────────────────────────────────────────────────────
    if any(w in t for w in ["what is trading","trading kya hai","trading kya h","trading kya hoti","trading kya hota","trading meaning","trading matlab","trading samjhao","trading explain","trading sikhna","trading kaise sikhe","trading kaise kare","trading kaise karu","trading kaise shuru","trading seekhna hai","trading start karna"]):
        if lang == "english":
            return bold(f"{E_CHART} What is Trading?\n\nTrading means buying something at a low price and selling it at a higher price!\n\nOn Quotex (Binary Trading):\n{E_CHECK} Just predict UP or DOWN\n{E_CHECK} Result in 1 minute\n{E_CHECK} Start from just $1\n{E_CHECK} 80-95% profit per trade\n\nExample:\n{E_RIGHT} Invest $10\n{E_RIGHT} Signal says UP\n{E_RIGHT} Place trade\n{E_RIGHT} Win = $18-19 in 1 minute! {E_PARTY}\n\nWith Wolf's VIP signals — a strong track record! {E_FIRE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")
        return bold(f"{E_CHART} Trading kya hai?\n\nTrading matlab — kisi cheez ko kam daam pe khareedna aur zyada daam pe bechna!\n\nQuotex pe Binary Trading:\n{E_CHECK} Sirf UP ya DOWN predict karo\n{E_CHECK} 1 minute mein result\n{E_CHECK} $1 se start\n{E_CHECK} 80-95% profit per trade\n\nExample:\n{E_RIGHT} $10 lagaye\n{E_RIGHT} Signal aaya UP\n{E_RIGHT} Trade lagaya\n{E_RIGHT} 1 min mein $18-19! {E_PARTY}\n\nWolf ke VIP signals ke saath — a strong track record! {E_FIRE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    if any(w in t for w in ["binary","binary trading","binary kya","binary options","options trading"]):
        if lang == "english":
            return bold(f"{E_BRAIN} Binary Trading!\n\nBinary = 2 options only:\n{E_CHART} UP — price will rise\n{E_CROSS} DOWN — price will fall\n\nIf correct:\n{E_MONEY} $10 invested → $18-19 back! (80-90% profit)\n\nIf wrong:\n{E_CROSS} Only lose $10\n\nThat's why Wolf's signals are gold — well-reviewed! {E_TARGET}")
        return bold(f"{E_BRAIN} Binary Trading!\n\nBinary = sirf 2 options:\n{E_CHART} UP — price badhegi\n{E_CROSS} DOWN — price giregi\n\nAgar sahi:\n{E_MONEY} $10 lagaye → $18-19 milenge!\n\nAgar galat:\n{E_CROSS} Sirf $10 jaayenge\n\nIsliye Wolf ke VIP signals chahiye — well-reviewed! {E_TARGET}")

    # ── SIGNALS ────────────────────────────────────────────────────────────────
    if any(w in t for w in ["signal basis","signal kaise banta","signal generate","signal analysis","signal kahan se","how signals made","signal kyon dete","signal kaise aata","signal kya hota","signal process"]):
        return bold(f"{E_SEARCH} Signals kaise generate hote hain?\n\nWolf analyse karte hain:\n{E_CHECK} Price Action\n{E_CHECK} Market news & analysis\n{E_CHECK} Candlestick movements\n{E_CHECK} Chart patterns\n{E_CHECK} Overall market setup\n\nSirf HIGH PROBABILITY setup confirm hone par signal dete hain! {E_TARGET}\n\nPehle Wolf khud trade lete hain, phir VIP ke saath share karte hain! {E_MUSCLE}")

    if any(w in t for w in ["khud analyse","personally analyse","har signal","every signal","signal khud","manually analyse"]):
        return bold(f"{E_MUSCLE} Haan bhai! 100% haan!\n\nWolf har signal personally analyse karte hain!\n\nAnalysis ke baad pehle khud trade lete hain aur wahi signals VIP members ke saath share karte hain! {E_TARGET}")

    if any(w in t for w in ["unstable market","volatile market","bad market","market kharab","market unstable"]):
        return bold(f"{E_SHIELD} Bilkul sahi poochha bhai!\n\nAgar market condition achhi nahi lagti toh us din trading bilkul avoid karo!\n\nYahi ek disciplined trader ki pehchaan hoti hai! {E_MUSCLE}\n\nthe team khud bhi aise din signals nahi dete! {E_FIRE}")

    if any(w in t for w in ["news time","news mein trade","news trading","news time signals","market news"]):
        return bold(f"{E_WARN} Generally the team news time mein trading recommend nahi karte!\n\nLekin agar news positive ho aur setup confirm lage toh kabhi kabhi trade dete hain!\n\nVIP mein News Time Trading Guidance bhi milti hai! {E_BAR}")

    if any(w in t for w in ["signal miss","miss ho gaya","signal miss kiya","late ho gaya","missed signal"]):
        return bold(f"{E_HAPPY} Signal miss ho gaya? Tension mat lo bhai!\n\nNext session ka wait karo!\n\nHar 1-2 ghante mein naye opportunities aati rehti hain! {E_CLOCK}\n\nVIP mein 5 sessions daily hain — kabhi kami nahi padegi! {E_FIRE}")

    if any(w in t for w in ["beginner signal","easy follow","simple signal","naye ke liye","beginner ke liye","simple hai kya"]):
        return bold(f"{E_CHECK} Bilkul! Wolf ke signals beginners ke liye bhi easy hain!\n\nWolf explain karte hain:\n{E_CHECK} Trade se pehle\n{E_CHECK} Trade ke dauraan\n{E_CHECK} Trade ke baad\n\nKoi experience nahi chahiye! {E_FIRE}")

    if any(w in t for w in ["signal se pehle analysis","pre analysis","market analysis share","pehle analysis"]):
        return bold(f"{E_BAR} Haan bhai!\n\nthe team trade dene se pehle, dauraan aur baad mein bhi analysis explain karte hain!\n\nVIP mein sab kuch transparent hai! {E_FIRE}")

    if any(w in t for w in ["manual trade","bot use","software","algo","automation","manually","khud trade karte","bot nahi"]):
        return bold(f"{E_MUSCLE} the team 100% MANUALLY trade karte hain!\n\nWoh 6+ saal se trading kar rahe hain!\n\nKoi bot, koi software, koi automation ki zaroorat nahi! {E_SMILE}\n\nPure skill aur experience — yahi unhe BEST banata hai! {E_TROPHY}")

    # ── VIP ────────────────────────────────────────────────────────────────────
    if any(w in t for w in ["vip mein kya","vip benefits","vip milta kya","vip access","vip membership","what is vip","vip group kya","vip mein milega"]):
        if lang == "english":
            return bold(f"{E_CROWN} What's inside VIP?\n\n{E_CHECK} Daily 10-20 Sureshot Trading Signals\n{E_CHECK} Daily 5-10 Compounding Signals\n{E_CHECK} 100% NON-Martingale trades\n{E_CHECK} 5 Sessions daily\n{E_CHECK} Daily Trading Guidance\n{E_CHECK} Market Analysis\n{E_CHECK} News Time Trading Guidance\n{E_CHECK} Proper Trading Education\n{E_CHECK} Risk Management\n{E_CHECK} Live Support 24/7\n{E_CHECK} Lifetime Access\n{E_CHECK} an active community\n\nJoin FREE — just deposit ${MIN_DEPOSIT}! {E_DIAMOND}\n\n{E_RIGHT} Register: {REGISTER_LINK}")
        return bold(f"{E_CROWN} VIP mein kya milta hai?\n\n{E_CHECK} Daily 10-20 Sureshot Trading Signals\n{E_CHECK} Daily 5-10 Compounding Signals\n{E_CHECK} 100% NON-Martingale trades\n{E_CHECK} 5 Sessions daily (Morning se Late Night)\n{E_CHECK} Daily Trading Guidance\n{E_CHECK} Market Analysis\n{E_CHECK} News Time Trading Guidance\n{E_CHECK} Proper Trading Education\n{E_CHECK} Risk Management\n{E_CHECK} Live Support 24/7\n{E_CHECK} Lifetime Access\n{E_CHECK} an active community\n\nFREE mein join karo — sirf ${MIN_DEPOSIT} deposit karo! {E_DIAMOND}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    if any(w in t for w in ["vip join karna","join vip","vip kaise join","how to join vip","vip mein kaise","vip join karna hai","vip lena hai","joining kaise kare","join kaise kare","kaise join karu","group join karna","vip group join","membership kaise","vip membership lena","abhi join karna"]):
        if lang == "english":
            return bold(f"{E_TROPHY} How to join VIP?\n\n3 simple steps:\n\n1{E_RIGHT} Register on Quotex using Wolf's referral link\n2{E_RIGHT} Deposit minimum ${MIN_DEPOSIT} (use WOLF50 for 50% bonus!)\n3{E_RIGHT} Send your Trader ID here\n\nVIP access granted instantly! {E_CHECK}\n\n{E_RIGHT} Register now: {REGISTER_LINK}")
        return bold(f"{E_TROPHY} VIP kaise join kare?\n\n3 simple steps:\n\n1{E_RIGHT} Wolf ke referral link se Quotex pe register karo\n2{E_RIGHT} Minimum ${MIN_DEPOSIT} deposit karo (WOLF50 = 50% bonus!)\n3{E_RIGHT} Apna Trader ID yahan bhejo\n\nVIP access turant mil jaayega! {E_CHECK}\n\n{E_RIGHT} Abhi register karo: {REGISTER_LINK}")

    if any(w in t for w in ["vip lifetime","lifetime","vip expire","vip kitne din","vip validity","vip ka time","vip kabhi expire"]):
        return bold(f"{E_PARTY} Khushkhabri bhai!\n\nWolf ka VIP LIFETIME hai!\n\nEk baar join karo — hamesha ke liye!\nKoi monthly fees nahi, koi renewal nahi! {E_DIAMOND}\n\nSirf ek baar ${MIN_DEPOSIT} deposit → Lifetime VIP! {E_FIRE}")

    if any(w in t for w in ["vip subscription","monthly plan","monthly fee","vip ka plan","subscription kya","monthly charge"]):
        return bold(f"{E_PARTY} VIP ka koi monthly subscription nahi hai bhai!\n\nSirf 3 steps:\n1{E_RIGHT} Mere referral link se account register karo\n2{E_RIGHT} Minimum ${MIN_DEPOSIT} deposit karo\n3{E_RIGHT} Apni trading journey mere saath start karo\n\nLifetime access — ek baar aur hamesha! {E_DIAMOND}")

    if any(w in t for w in ["vip join ke baad kitne","same day","turant signal","kitne time mein signal","vip join ke baad pehla","first step vip","vip ke baad kya","vip join karne ke baad signal","signal kab milega","kitna time lagta","kitne time mein milega","jaldi milega","turant milega","vip join hone ke baad"]):
        return bold(f"{E_PARTY} VIP join karne ke baad!\n\nSame day se access milta hai! {E_CHECK}\nUsi din 5 sessions aur 10+ signals shuru!\n\nPehla step:\n1{E_RIGHT} Samjho ki Wolf signals kaise dete hain\n2{E_RIGHT} Demo account par signals try karo\n3{E_RIGHT} Confidence aane ke baad live pe aao\n\nDiscipline follow karo! {E_MUSCLE}")

    if any(w in t for w in ["vip rules","rules kya","koi rules","group rules","vip ke rules","rules batao"]):
        return bold(f"{E_HAPPY} VIP ke rules bahut simple hain bhai!\n\nBas:\n{E_CHECK} Join karo\n{E_CHECK} Discipline maintain karo\n{E_CHECK} Signals properly follow karo\n\nKoi complicated rules nahi! {E_FIRE}")

    if any(w in t for w in ["vip transfer","transfer karna","dusre ko de sakta","vip share","vip dusre ko"]):
        return bold(f"{E_CROSS} Nahi bhai! VIP transfer nahi ho sakta!\n\nJo account tumne mere referral link se banaya hai — VIP access usi ke liye valid hai!\n\nHar person ko apna account register karna hoga! {E_CHECK}")

    if any(w in t for w in ["galat id","wrong id","galti se id","wrong trader id","id galat bheja"]):
        return bold(f"{E_HAPPY} Tension mat lo bhai!\n\nAgar galti se wrong Trader ID bhej di hai toh correct ID dubara bhejo!\n\nKoi problem nahi hogi! {E_CHECK}\n\nTrader ID: Quotex → My Account → 8-digit number {E_KEY}")

    if any(w in t for w in ["orientation","guide milegi","vip guide","tutorial vip","how to start vip","new members guidance","naye members","new member ko","joining ke baad","naya member","guidance milegi","welcome guidance"]):
        return bold(f"{E_CHECK} Haan bhai! VIP join karne ke baad guidance milti hai!\n\nthe team recommend karte hain ki pehle demo pe signals test karo!\n\nSamjho ki woh trades kaise dete hain — phir live start karo! {E_MUSCLE}")

    if any(w in t for w in ["advanced strategy","advanced sikho","advanced trading","price action advanced","experienced trader","advanced strategies vip","advanced milta","vip advanced"]):
        return bold(f"{E_CHART} Haan bilkul! VIP mein advanced bhi milta hai!\n\n{E_CHECK} Advanced strategies\n{E_CHECK} Price Action in detail\n{E_CHECK} Candlestick Patterns\n{E_CHECK} Chart Patterns\n{E_CHECK} Market Psychology\n\nSab kuch sikhate hain the team! {E_FIRE}{E_DIAMOND}")

    if any(w in t for w in ["account block","blocked account","quotex block","help with block","account band"]):
        return bold(f"{E_HAPPY} Account block? Tension mat lo bhai!\n\nthe team poori help karne ki koshish karte hain!\n\nQuotex management ke contacts ke through bhi support dilane ki koshish ki jaati hai!\n\nAbhi contact karo: {SUPPORT_USER} {E_CHAT}")

    # ── SIGNALS TIMING ─────────────────────────────────────────────────────────
    if any(w in t for w in ["signals kab","signal timing","session timing","session kab","kab milte","signal time","trading time","best time","session schedule","5 session","sessions kab","best trading time","sabse acha time","optimal time","3 pm","peak time","prime time"]):
        if lang == "english":
            return bold(f"{E_CLOCK} Trading Session Timings\n\nWolf gives signals in 5 sessions:\n\n{E_SUN} Morning: 8 AM – 10 AM\n{E_CLOUD} Afternoon: 12 PM – 2 PM\n{E_SUNSET} Evening: 4 PM – 6 PM\n{E_MOON} Night: 8 PM – 10 PM\n{E_NIGHT} Late Night: 10 PM – 1 AM\n\nTotal: 10-20 signals daily! {E_CHART}\n24x7, 365 days! {E_CHECK}\n\nBest time: 3 PM – 9 PM {E_TARGET}\nMore opportunities on Quotex during this time!")
        return bold(f"{E_CLOCK} Trading Session Timings\n\nWolf 5 sessions mein signals dete hain:\n\n{E_SUN} Morning: 8 AM – 10 AM\n{E_CLOUD} Afternoon: 12 PM – 2 PM\n{E_SUNSET} Evening: 4 PM – 6 PM\n{E_MOON} Night: 8 PM – 10 PM\n{E_NIGHT} Late Night: 10 PM – 1 AM\n\nTotal: Daily 10-20 signals! {E_CHART}\n24x7, 365 din! {E_CHECK}\n\nBest time: 3 PM – 9 PM {E_TARGET}\nIs time par Quotex pe zyada opportunities milti hain!")

    if any(w in t for w in ["weekend","saturday","sunday","shanivaar","ravivar","weekend signals","weekends pe"]):
        return bold(f"{E_FIRE} Haan bhai! 24x7, 365 din!\n\nWeekends pe bhi signals milte hain!\n\nWolf roz trade karte hain! {E_CHART}\n\nVIP join karo aur kabhi signal miss mat karo! {E_DIAMOND}")

    # ── ACCURACY ───────────────────────────────────────────────────────────────
    if any(w in t for w in ["kitni accuracy hai","signal accuracy","win rate","success rate","kitna sahi","how accurate","accuracy kitni","percent accuracy","hit rate","signal sahi hote","kya signal accurate","signal sahi","kitna correct","accuracy kya"]):
        return bold(f"{E_TARGET} Signal Accuracy\n\nAverage: 93% – 96%\nKabhi kabhi: 99% tak! {E_FIRE}\n\nSimple words mein:\n10 signals diye → 9-10 winning! {E_CHECK}\n\nQuotex ke official page par verified! {E_TROPHY}\n\nVIP join karo aur khud experience karo! {E_DIAMOND}")

    # ── CURRENCY PAIRS ─────────────────────────────────────────────────────────
    if any(w in t for w in ["currency pair","otc","live market","kaunsi pairs","which pairs","currency","pairs pe signal","otc pairs"]):
        return bold(f"{E_BAR} the team dono OTC aur Live Market pairs par signals provide karte hain!\n\nSabse popular:\n{E_CHECK} EUR/USD\n{E_CHECK} USD/JPY\n{E_CHECK} GBP/USD\n{E_CHECK} AUD/USD\n{E_CHECK} OTC pairs\n\nVIP mein specific pairs daily bataye jaate hain! {E_FIRE}")

    # ── MARTINGALE ─────────────────────────────────────────────────────────────
    if any(w in t for w in ["martingale","non martingale","martingale use","martingale kya","martingale strategy","non-mtg","non mtg"]):
        return bold(f"{E_SHIELD} the team generally NON-Martingale approach prefer karte hain! {E_CHECK}\n\nLekin jab koi setup bahut strong aur highly confirmed lage toh kabhi kabhi Martingale bhi use karte hain!\n\nVIP mein Non-Martingale strategy sikhate hain — safest approach! {E_MUSCLE}")

    # ── DEPOSIT ────────────────────────────────────────────────────────────────
    if any(w in t for w in ["deposit","minimum deposit","kitna deposit","how much deposit","deposit karna","deposit kaise","amount deposit","deposit method","deposit karo","kitna lagana","paise kaise dalein","payment kaise","kitna time lagta deposit","minimum amount","kam se kam kitna","paisa dalna","paise bhejne"]):
        if lang == "english":
            return bold(f"{E_MONEY} Deposit Information!\n\nMinimum: ${MIN_DEPOSIT}\n\nPro tip — use WOLF50:\n{E_GIFT} 50% bonus on your deposit!\n\nDeposit methods:\n{E_CHECK} UPI / Paytm / PhonePe\n{E_CHECK} Net Banking\n{E_CHECK} Cryptocurrency\n{E_CHECK} Trust Wallet\n\nRecommended: $100 (Rs.8,000)\n\n{E_RIGHT} Register: {REGISTER_LINK}")
        return bold(f"{E_MONEY} Deposit ki jaankari!\n\nMinimum: ${MIN_DEPOSIT}\n\nPro tip — WOLF50 use karo:\n{E_GIFT} 50% bonus milta hai deposit par!\n\nDeposit methods:\n{E_CHECK} UPI / Paytm / PhonePe\n{E_CHECK} Net Banking\n{E_CHECK} Cryptocurrency\n{E_CHECK} Trust Wallet\n\nRecommended: $100 (Rs.8,000)\n\n{E_RIGHT} Abhi register karo: {REGISTER_LINK}")

    if any(w in t for w in ["bonus","promo","code","discount","offer","noah50","50 percent","50%","bonus code","promo code","coupon"]):
        if lang == "english":
            return bold(f"{E_GIFT} 50% Bonus Code!\n\nCode: \n\nHow to use:\n1{E_RIGHT} Go to deposit on Quotex\n2{E_RIGHT} Enter amount\n3{E_RIGHT} Enter promo code: \n4{E_RIGHT} Get 50% extra balance! {E_PARTY}\n\n{E_WARN} Only for accounts registered through Wolf's link!\n\n{E_RIGHT} Register: {REGISTER_LINK}")
        return bold(f"{E_GIFT} 50% Bonus Code!\n\nCode: \n\nKaise use kare:\n1{E_RIGHT} Quotex pe deposit section mein jao\n2{E_RIGHT} Amount daalo\n3{E_RIGHT} Promo code: \n4{E_RIGHT} 50% extra balance! {E_PARTY}\n\n{E_WARN} Sirf Wolf ke link se register karne walo ko milega!\n\n{E_RIGHT} Abhi register karo: {REGISTER_LINK}")

    if any(w in t for w in ["recommend amount","kitne se start","starting amount","best amount","kitna lagaye","how much start","investment amount","kitna invest","ideal amount"]):
        return bold(f"{E_CASH} Kitne se start kare?\n\nMinimum: ${MIN_DEPOSIT} (VIP unlock)\nRecommended: $100 (Rs.8,000)\n\nKyun $100?\n{E_CHECK} Proper money management\n{E_CHECK} Better growth\n{E_CHECK} 1% risk = $1 per trade\n\nResults vary depending on capital, session and market — never guaranteed.\n\nSmart start karo! {E_CHART}")

    # ── WITHDRAWAL ─────────────────────────────────────────────────────────────
    if any(w in t for w in ["withdraw","withdrawal","nikalna","paise nikalo","nikaalna","nikaal","cash out","paise bahar","withdrawal kaise","paise kaise nikale","nikalna kaise","kitne din mein","kab tak milega paise","withdrawal time","paise kab aayenge","payment kab milega"]):
        if lang == "english":
            return bold(f"{E_PLANE} Withdrawal Process!\n\nWithdrawal on Quotex is very easy:\n\n1{E_RIGHT} Open Quotex app\n2{E_RIGHT} Go to Withdraw section\n3{E_RIGHT} Enter amount\n4{E_RIGHT} Choose payment method\n5{E_RIGHT} Done!\n\nTime:\n{E_BOLT} Crypto/Trust Wallet: Sometimes 10 minutes!\n{E_BANK} UPI/Paytm/PhonePe: 24-48 hours\n\nMinimum withdrawal: Rs.1,000\n\n{E_CHECK} No withdrawal problems for VIP members! {E_FIRE}")
        return bold(f"{E_PLANE} Withdrawal Process!\n\nQuotex pe withdrawal bahut easy hai:\n\n1{E_RIGHT} Quotex app open karo\n2{E_RIGHT} Withdraw section mein jao\n3{E_RIGHT} Amount daalo\n4{E_RIGHT} Payment method choose karo\n5{E_RIGHT} Done!\n\nTime:\n{E_BOLT} Crypto/Trust Wallet: Kabhi kabhi 10 minute!\n{E_BANK} UPI/Paytm/PhonePe: 24-48 ghante\n\nMinimum: Rs.1,000\n\n{E_CHECK} VIP members ka koi withdrawal problem nahi! {E_FIRE}")

    # ── QUOTEX ─────────────────────────────────────────────────────────────────
    if any(w in t for w in ["quotex kya","about quotex","quotex safe","quotex real","quotex app","quotex platform","quotex kaise","quotex kaisa","broker kya","trading platform","quotex download","app kaise download","quotex install","platform download","app download kaise","quotex app download"]):
        return bold(f"{E_PHONE} Quotex ke baare mein!\n\nQuotex ek online binary trading platform hai:\n\n{E_CHECK} UP ya DOWN predict karo\n{E_CHECK} Sirf $1 se start\n{E_CHECK} 80-95% profit per trade\n{E_CHECK} Instant withdrawal\n{E_CHECK} 24/7 available\n{E_CHECK} Mobile app available (Android + iOS download karo!)\n{E_CHECK} Free demo account\n{E_CHECK} UPI, Paytm, Crypto deposits\n\nApp download: Play Store / App Store mein Quotex search karo!\n\nthe team personally Quotex use aur recommend karte hain! {E_FIRE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    if any(w in t for w in ["kyc","verification","kyc kaise","kyc zaroori","verify account","account verify","kyc important"]):
        return bold(f"{E_KEY} KYC Verification!\n\nHaan bhai, Quotex pe KYC important hai!\n\nKyun:\n{E_CHECK} Withdrawal ke liye zaroori\n{E_CHECK} Age verification\n{E_CHECK} Account security\n\nKYC ke baad — withdrawal smoothly! {E_PLANE}\n\nRegister karne ke baad jaldi KYC complete karo! {E_FIRE}")

    if any(w in t for w in ["demo","demo account","practice","demo trading","try karna","pehle try","demo pe","free demo","demo se start"]):
        return bold(f"{E_GAME} Demo Account!\n\nHaan! Quotex pe FREE demo account milta hai!\n\nWolf recommend karte hain:\n1{E_RIGHT} Pehle demo pe signals try karo\n2{E_RIGHT} Wolf ki trading style samjho\n3{E_RIGHT} Confidence aane ke baad live pe shift ho\n\nPractice se hi perfection! {E_MUSCLE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    if any(w in t for w in ["quotex legal","legal hai","india mein legal","india legal","legal nahi","legal kya"]):
        return bold(f"{E_LEGAL} Quotex Legal Status!\n\nthe team ke personal opinion mein — agar platform itne saalon se chal raha hai aur lakhon users use kar rahe hain toh log khud research karke decision le sakte hain!\n\nQuotex pe lakhs of Indians trade karte hain!\n\nApna research zaroor karo! {E_CHECK}")

    # ── REGISTRATION ───────────────────────────────────────────────────────────
    if any(w in t for w in ["register","registration","sign up","signup","account banao","account kaise","account banana","kaise banaye","new account","create account","account kholna"]):
        if lang == "english":
            return bold(f"{E_KEY} How to Register?\n\nStep by step:\n\n1{E_RIGHT} Click Wolf's referral link\n2{E_RIGHT} Sign up with email or Google\n3{E_RIGHT} Verify account\n4{E_RIGHT} Deposit minimum ${MIN_DEPOSIT}\n5{E_RIGHT} Use code WOLF50 (50% bonus)\n6{E_RIGHT} Copy your Trader ID\n7{E_RIGHT} Send it here\n\nVIP access instantly! {E_TROPHY}\n\n{E_RIGHT} Register: {REGISTER_LINK}")
        return bold(f"{E_KEY} Register kaise kare?\n\nStep by step:\n\n1{E_RIGHT} Wolf ke referral link pe click karo\n2{E_RIGHT} Email ya Google se sign up karo\n3{E_RIGHT} Account verify karo\n4{E_RIGHT} Minimum ${MIN_DEPOSIT} deposit karo\n5{E_RIGHT} Code WOLF50 use karo (50% bonus)\n6{E_RIGHT} Apna Trader ID copy karo\n7{E_RIGHT} Yahan bhejo\n\nVIP access turant! {E_TROPHY}\n\n{E_RIGHT} Abhi register karo: {REGISTER_LINK}")

    if any(w in t for w in ["trader id","id kahan","id kaha","id kaise","find id","id dhundna","id milega","8 digit","trader id kahan","id copy karo"]):
        return bold(f"{E_KEY} Trader ID kaise dhundhein?\n\n1{E_RIGHT} Quotex app ya website open karo\n2{E_RIGHT} Top left mein profile icon click karo\n3{E_RIGHT} 'My Account' mein jao\n4{E_RIGHT} 8-digit number dikhega — wahi Trader ID hai!\n\nExample: 12345678\n\nCopy karke yahan bhejo aur VIP unlock karo! {E_CHECK}")

    # ── HOW TO EARN ────────────────────────────────────────────────────────────
    if any(w in t for w in ["paise kaise","earn kaise","income kaise","paisa kaise","money kaise","kamana","kamayi","earning","kaise kamaye","paise kamao","how to earn","how to make money","make money","paisa banana","daily income","passive income","kitna kamaoonga","invest karna","paisa lagana","paise lagana","mujhe join karna","joining kaise","kaise join","join kaise karu"]):
        if lang == "english":
            return bold(f"{E_MONEY} How to earn with trading?\n\nSimple steps:\n\n1{E_RIGHT} Register on Quotex with Wolf's link\n2{E_RIGHT} Deposit minimum ${MIN_DEPOSIT} (WOLF50 = 50% bonus!)\n3{E_RIGHT} Join VIP\n4{E_RIGHT} Get Wolf's signals\n5{E_RIGHT} Place trades and earn profit! {E_PARTY}\n\nVIP members earn variable, never guaranteed, amounts! {E_CHART}\n\n{E_RIGHT} Register: {REGISTER_LINK}")
        return bold(f"{E_MONEY} Trading se paise kaise kamaye?\n\nStep by step:\n\n1{E_RIGHT} Quotex pe Wolf ke link se account banao\n2{E_RIGHT} Minimum ${MIN_DEPOSIT} deposit karo (WOLF50 = 50% bonus!)\n3{E_RIGHT} VIP join karo\n4{E_RIGHT} Wolf ke signals aate hain\n5{E_RIGHT} Trade lagao aur profit karo! {E_PARTY}\n\nVIP members daily trading with variable, never guaranteed, results! {E_CHART}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    # ── PROOF & RESULTS ────────────────────────────────────────────────────────
    if any(w in t for w in ["proof","results","dikhao","show proof","evidence","result dikhao","withdrawal proof","profit proof","show results","proof chahiye","proof kahan"]):
        return bold(f"{E_SEARCH} Proof chahiye? Lo!\n\n{E_CHECK} Live trading recordings\n{E_CHECK} Members ke testimonials\n{E_CHECK} Daily results public channel pe\n\n{E_TV} Public Channel: {TG_CHANNEL}")

    if any(w in t for w in ["daily results","results share","trading results","live results","result kahan","result dekhna","aaj ka result"]):
        return bold(f"{E_BAR} Haan bhai! the team daily Trading Wolf Public Channel par trading results share karte hain!\n\n{E_TV} Public Channel:\n{TG_CHANNEL}\n\nWahan dekh sakte ho:\n{E_CHECK} Daily signal results\n{E_CHECK} Win/Loss transparency\n{E_CHECK} Withdrawal proofs\n{E_CHECK} Member feedback")

    if any(w in t for w in ["testimonials","member feedback","reviews","members ka feedback","log kya kehte","success stories"]):
        return bold(f"{E_TROPHY} Testimonials available hain bhai!\n\nPublic Channel par:\n{E_CHECK} Member videos\n{E_CHECK} Profit screenshots\n{E_CHECK} Withdrawal proofs\n{E_CHECK} Real feedback\n\n{E_TV} Check karo: {TG_CHANNEL}")

    if any(w in t for w in ["live recordings","live session recording","recording dekhna","session recording","live trading recording"]):
        return bold(f"{E_VIDEO} Haan bhai! Live sessions ki recordings bhi share ki jaati hain!\n\nVIP mein:\n{E_CHECK} Live trading sessions\n{E_CHECK} Session recordings\n\nPublic channel mein bhi free content milta hai!\n\n{E_TV} Join karo: {TG_CHANNEL}")

    if any(w in t for w in ["student success","zero se start","student profitable","sabse successful student","60 lakh","15 saal"]):
        return bold(f"{E_STAR} Hamare kai successful members hain!\n\nZero se start karke, discipline ke saath dheere dheere progress kiya hai! {E_FIRE}\n\nProof community mein available hai!\n\nAgar woh kar sakte hain — tum bhi kar sakte ho! {E_MUSCLE}")

    # ── GUARANTEED & LOSS ──────────────────────────────────────────────────────
    if any(w in t for w in ["guaranteed","guarantee","pakka","sure profit","100 percent profit","guaranteed profit","pakka profit","sure shot profit","fixed profit"]):
        return bold(f"{E_PRAY} Honest jawab!\n\nNAHI — Wolf kabhi guaranteed profit ka claim nahi karte!\n\nTrading mein hamesha profit aur loss dono possibilities hoti hain!\n\nWolf kya dete hain:\n{E_CHECK} Har member ke liye 100% best\n{E_CHECK} a strong track record\n{E_CHECK} Transparent results\n{E_CHECK} Proper risk management\n\nHonest trading = Long-term success! {E_MUSCLE}")

    if any(w in t for w in ["loss hua","loss ho gaya","paise gaye","loss responsibility","loss ka zimma","loss cover","nuksaan","loss ke baad kya","loss recover karna","loss recovery","loss cover hoga","paise wapas","loss se kaise","loss ho raha","ghata hua","kitna loss"]):
        return bold(f"{E_MUSCLE} Loss ke baare mein!\n\nTrading mein profit aur loss dono part hain!\n\nLoss hua toh:\n{E_CHECK} Us din trading band karo\n{E_CHECK} Quotex logout karo\n{E_CHECK} Next day fresh mind se aao\n{E_CROSS} Revenge trading bilkul mat karo!\n\nResponsibility khud trader ki hoti hai!\n\nWolf recovery mein help karte hain! {E_FIRE}")

    # ── REFERRAL ───────────────────────────────────────────────────────────────
    if any(w in t for w in ["referral","commission","referral se","tumhara fayda","kya fayda","aapko kya","noah ko kya milta","referral income"]):
        return bold(f"{E_PRAY} Referral ke baare mein honest jawab!\n\nHaan, hume chhota sa referral commission milta hai jab tum hamare link se register karte ho!\n\nLekin unka MAIN goal:\n{E_CHECK} Strong trading community build karna\n{E_CHECK} FREE mein zyada logon ko sikhana\n{E_CHECK} Sirf serious traders hi join karein!\n\nUnka mission referral se bada hai! {E_MUSCLE}")

    # ── EDUCATION ──────────────────────────────────────────────────────────────
    if any(w in t for w in ["beginner","naya hoon","naye hain","beginner hoon","new to trading","trading nahi aati","kuch nahi aata","zero knowledge","pehli baar","beginner kya kare","newbie","experience chahiye","experience nahi","naye logon ke liye","experience nahi hai","pehle kabhi nahi kiya","shuru kaise karu","kaise shuru karu","kahan se shuru","doubt hai","koi doubt","sahi hai kya","mere liye sahi","suggest karo","kya karoon","kya karu"]):
        if lang == "english":
            return bold(f"{E_BOOK} New to trading? No worries!\n\nStep 1: Watch Wolf's YouTube for basics\nStep 2: Start with demo account (free!)\nStep 3: Join VIP for live guidance\n\nLearn these first:\n{E_BOOK} Money Management\n{E_BOOK} Risk Management\n{E_BOOK} Chart Patterns\n{E_BOOK} Candlestick Patterns\n{E_BOOK} Price Action\n\n{E_TV} Free Course: {COURSE_LINK}\n\nWolf helps beginners! {E_MUSCLE}")
        return bold(f"{E_BOOK} Trading bilkul nahi aati? Tension mat lo!\n\nStep 1: Wolf ke YouTube se basics seekho\nStep 2: Demo account se practice karo\nStep 3: VIP join karo live guidance ke liye\n\nPehle yeh seekho:\n{E_BOOK} Money Management\n{E_BOOK} Risk Management\n{E_BOOK} Chart Patterns\n{E_BOOK} Candlestick Patterns\n{E_BOOK} Price Action\n\n{E_TV} Free Course: {COURSE_LINK}\n\nWolf beginners ko special guidance dete hain! {E_MUSCLE}")

    if any(w in t for w in ["sabse pehle kya","first step","pehle kya","what to learn first","trading start kahan","shuru kahan se"]):
        return bold(f"{E_BOOK} Pehle yeh seekho bhai!\n\n1{E_RIGHT} Money Management\n2{E_RIGHT} Risk Management\n3{E_RIGHT} Chart Patterns\n4{E_RIGHT} Candlestick Patterns\n5{E_RIGHT} Price Action\n\nYeh basics strong hone ke baad signals follow karo!\n\n{E_TV} Free Course: {COURSE_LINK}\n\nthe team VIP mein yeh sab sikhate hain! {E_FIRE}")

    if any(w in t for w in ["price action","support resistance","breakout","reversal","sr levels"]):
        return bold(f"{E_BAR} Price Action Trading!\n\nPrice Action mein mainly:\n{E_CHECK} Support & Resistance identify karna\n{E_CHECK} Breakouts trade karna\n{E_CHECK} Reversals catch karna\n\nMarket ke major reaction zones aur rejection levels analyse karke S/R identify kiye jaate hain!\n\nthe team VIP mein detail mein sikhate hain! {E_FIRE}{E_DIAMOND}")

    if any(w in t for w in ["candlestick","candle pattern","candlestick pattern","candles","candle kya"]):
        return bold(f"{E_BOOK} Candlestick Patterns!\n\nCandlestick patterns market ki psychology samajhne mein bahut important hote hain!\n\nPopular patterns:\n{E_CHECK} Doji\n{E_CHECK} Hammer\n{E_CHECK} Engulfing\n{E_CHECK} Pin Bar\n{E_CHECK} Inside Bar\n\nthe team VIP mein in sab ko practically sikhate hain! {E_FIRE}")

    if any(w in t for w in ["compounding","compounding kya","compound","compound kya","reinvest","grow account","compounding strategy"]):
        return bold(f"{E_CHART} Compounding Strategy!\n\nMeaning: Profit ko systematically reinvest karna aur account step-by-step grow karna!\n\nExample:\nStart: Rs.5,000\n1 hafte baad: Rs.7,500\nReinvest → 2 hafte: Rs.11,000\nReinvest → 1 mahina: Rs.25,000+\n\nYahi hai idea behind compounding — though real results always vary. {E_FIRE}\n\nWolf VIP mein yeh sikhate hain! {E_DIAMOND}")

    if any(w in t for w in ["risk management","money management","risk kaise","manage risk","stop loss","kitna risk","risk kitna le"]):
        return bold(f"{E_SHIELD} Risk Management!\n\nGolden rule: Sirf 1% per trade risk karo!\n\nExample ($100 account):\n{E_CHECK} Per trade risk: sirf $1\n{E_CHECK} 5 trades bhi lose — safe rahoge!\n\nHar session se pehle Wolf batate hain:\n{E_BAR} Aaj kitna risk\n{E_TARGET} Profit target\n\nYahi winners aur losers ka fark hai! {E_MUSCLE}")

    if any(w in t for w in ["psychology","trading psychology","emotion","emotional","dar","fear","greed","laalach","patience","sabr","emotional trading"]):
        return bold(f"{E_BRAIN} Trading Psychology!\n\nTrading ka SABSE IMPORTANT part!\n\nCommon galtiyan:\n{E_CROSS} Dar ke saath trading\n{E_CROSS} Jeenne ke baad greed\n{E_CROSS} Loss ke baad revenge trading\n{E_CROSS} Overtrading\n\nWolf ke rules:\n{E_CHECK} Loss → stop, logout, kal aao\n{E_CHECK} Profit target hit → us din band!\n{E_CHECK} Emotional hoke kabhi trade mat karo!\n\nVIP mein psychology bhi sikhate hain! {E_MUSCLE}")

    if any(w in t for w in ["overtrading","bahut zyada trade","too many trades","bade trade","badi amount"]):
        return bold(f"{E_STOP} Overtrading se kaise bachen?\n\nSimple rule:\nProfit target ya stop-loss hit hote hi trading stop karo aur account logout karo!\n\nDiscipline hi trading mein safalta ki key hai! {E_MUSCLE}\n\nthe team yeh VIP mein sikhate hain! {E_FIRE}")

    # ── PROFIT EXPECTATIONS ────────────────────────────────────────────────────
    if any(w in t for w in ["pehle month","first month","1 month profit","monthly profit","ek mahine","mahine mein","month mein kitna","monthly income"]):
        return bold(f"{E_MONEY} Pehle mahine mein kitna profit?\n\nDiscipline ke saath:\nRs.1,000 se start → results vary and are never guaranteed.\n\n{E_CHECK} Proper compounding\n{E_CHECK} Saare signals follow\n{E_CHECK} Risk management\n{E_CHECK} Emotional mat ho\n\nResult tumhare discipline pe depend karta hai! {E_CHART}")

    if any(w in t for w in ["20 dollar","$20 profit","20 se profit","minimum se profit","chhoti amount profit"]):
        return bold(f"{E_CASH} Honestly bhai — chhoti amount se bhi profit ho sakta hai, lekin results kabhi guaranteed nahi hote! {E_CHECK}\n\nChhoti amount mein:\n{E_WARN} Risk zyada hota hai\n{E_WARN} Growth room comparatively kam\n\nRecommended start: $100 (Rs.8,000)\n\nJitna zyada capital, utna zyada room to grow — but risk bhi utna hi real hai! {E_CHART}")

    # ── COMMUNITY ──────────────────────────────────────────────────────────────
    if any(w in t for w in ["youtube","videos dekho","yt link","youtube pe","youtube channel","subscribe"]):
        return bold(f"{E_TV} Trading Wolf YouTube Channel!\n\n{E_RIGHT} {YOUTUBE}\n\nWahan milega:\n{E_CHECK} Free trading tutorials\n{E_CHECK} Live trading videos\n{E_CHECK} Success stories & proofs\n{E_CHECK} Market analysis\n{E_CHECK} Strategy videos\n\nAbhi: a growing YouTube audience! {E_TROPHY}\n\n{E_RIGHT} Free Course: {COURSE_LINK}\n\nSubscribe karo! {E_FIRE}")

    if any(w in t for w in ["course","playlist","basic to advance","trading course","free course","sikho trading"]):
        return bold(f"{E_BOOK} Trading Wolf Free Course!\n\nBasic to Advanced — bilkul FREE!\n\n{E_RIGHT} Course Link:\n{COURSE_LINK}\n\nIs course mein milega:\n{E_CHECK} Trading basics\n{E_CHECK} Chart patterns\n{E_CHECK} Candlestick patterns\n{E_CHECK} Price action\n{E_CHECK} Risk management\n\nSabse pehle yeh course dekho! {E_FIRE}")

    if any(w in t for w in ["channel link","telegram link","public channel","community","group link","telegram channel","channel kahan","telegram group"]):
        return bold(f"{E_PHONE} Trading Wolf Telegram Community!\n\n{E_RIGHT} Public Channel:\n{TG_CHANNEL}\n\nCommunity: a growing Telegram community! {E_TROPHY}\n\nPublic channel mein:\n{E_CHECK} Daily trading results\n{E_CHECK} Free signals\n{E_CHECK} Member testimonials\n{E_CHECK} Live session updates\n\nAbhi join karo! {E_FIRE}")

    if any(w in t for w in ["personally respond","noah respond","aap personally","direct reply","noah khud reply","noah personally"]):
        return bold(f"{E_CHAT} Jitna possible hota hai the team personally respond karte hain!\n\nSaath hi 24x7 human support team bhi available hai!\n\nHar member equal hai — VIP ya non-VIP mein koi fark nahi support ke liye! {E_CHECK}\n\n{E_RIGHT} {SUPPORT_USER}")

    if any(w in t for w in ["live session","live trading session","daily session","session hota hai","live hote hain","live classes"]):
        return bold(f"{E_PHONE} Haan bhai! the team daily VIP group aur public group dono mein live sessions lete hain!\n\nJahan:\n{E_CHECK} Free trading education\n{E_CHECK} Live signals share\n{E_CHECK} Market analysis\n{E_CHECK} Q&A\n\nVIP mein 5 daily sessions hote hain! {E_FIRE}")

    # ── FEES ───────────────────────────────────────────────────────────────────
    if any(w in t for w in ["free hai","koi fees","kuch charge","paid hai","fees lagti","charge karte","pay karna","course fees","vip fees","free mein milega","free hai kya","bilkul free","free wala","kitna paisa lagega","kya free hai"]):
        return bold(f"{E_PARTY} Sab FREE hai bhai!\n\nthe team zindagi bhar FREE mein guidance aur educational content dete rahenge!\n\nKoi paid course nahi, koi force nahi!\n\nSirf: Wolf ke link se Quotex pe register karo + ${MIN_DEPOSIT} deposit karo\n\nWoh deposit tumhara apna trading capital hai — koi fee nahi! {E_MUSCLE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")

    if any(w in t for w in ["baad mein charge","future charge","baad mein fees","future mein paid","course bechoge","koi hidden"]):
        return bold(f"{E_CHECK} Nahi bhai! 100% nahi!\n\nthe team kabhi force karke koi paid course nahi bechenge!\n\nUnka goal sirf: FREE mein trading sikhana aur community build karna! {E_MUSCLE}")

    # ── THANKS / CLOSING ───────────────────────────────────────────────────────
    if any(w in _words for w in ["thanks","shukriya","dhanyawad","ty","thx","thanku"]) or "thank you" in t or "thank u" in t:
        if lang == "english":
            return bold(f"{E_HAPPY} You're welcome!\n\nFeel free to ask anything anytime!\n\nReady to join VIP? Register now {E_FIRE}\n\n{E_RIGHT} {REGISTER_LINK}")
        return bold(f"{E_HAPPY} Anytime bhai!\n\nKoi bhi sawaal ho toh poochho — main hamesha yahan hoon!\n\nVIP join karna hai? Abhi register karo {E_FIRE}\n\n{E_RIGHT} {REGISTER_LINK}")

    if any(w in _words for w in ["bye","goodbye","alvida","tc"]) or any(w in t for w in ["baad mein","phir milenge","take care","chal bhai"]):
        return bold(f"{E_WAVE} Bye bhai!\n\nKabhi bhi wapas aana!\n\nVIP join karna mat bhoolo! {E_FIRE}{E_DIAMOND}")

    if any(w in _words for w in ["ok","okay","theek","thik","alright","haan","han","accha","acha","sure"]) or any(w in t for w in ["ji haan","got it","samajh gaya","theek hai"]):
        return bold(f"{E_THUMB} Perfect bhai!\n\nAur koi sawaal?\n\nVIP join karne ke liye ready ho? {E_FIRE}\n\n{E_RIGHT} Register: {REGISTER_LINK}")


    # ── DEFAULT ────────────────────────────────────────────────────────────────
    if lang == "english":
        return bold(f"{E_HAPPY} Thanks for your message!\n\nFor detailed help, contact: {SUPPORT_USER}\n\nOr I can help you with:\n{E_CHART} What is trading\n{E_CROWN} How to join VIP\n{E_MONEY} Deposit & bonus info\n{E_LION} About the team (Trading Wolf)\n{E_TV} YouTube: {YOUTUBE}\n{E_CLOCK} Signal timings\n\nWhat would you like to know? {E_FIRE}")
    return bold(f"{E_HAPPY} Bhai!\n\nZyada detail ke liye contact karo: {SUPPORT_USER}\n\nYa main help kar sakta hoon:\n{E_CHART} Trading kya hai\n{E_CROWN} VIP kaise join kare\n{E_MONEY} Deposit & bonus\n{E_LION} the team ke baare mein\n{E_TV} YouTube: {YOUTUBE}\n{E_CLOCK} Signal timings\n\nKya jaanna chahte ho? {E_FIRE}")

async def ask_gemini(chat_id: int, user_text: str) -> str:
    reply = smart_reply(user_text)
    print(f"✅ Smart reply sent to chat {chat_id}")
    return reply


async def handle_postback(request):
    """Receives postbacks directly from Quotex"""
    from aiohttp import web
    params = request.rel_url.query

    # Reject anyone who doesn't supply the correct secret key.
    if params.get("key", "") != POSTBACK_KEY:
        return web.Response(text="Forbidden", status=403)

    def get_real(key):
        val = params.get(key, "").strip()
        if val.startswith("{") and val.endswith("}"):
            return ""
        return val
    
    uid     = get_real("uid")
    status  = get_real("status")
    # Bulletproof amount parsing: a malformed sumdep from the affiliate network
    # must NEVER crash the request (a crashed postback is money lost forever).
    _raw_dep = get_real("sumdep")
    try:
        sumdep = float(str(_raw_dep).replace(",", ".").strip() or 0)
    except (ValueError, TypeError):
        print(f"⚠️ Unparseable sumdep {_raw_dep!r} for uid={uid} — treating as 0")
        sumdep = 0.0
    country = get_real("country") or "N/A"
    
    print(f"POSTBACK: uid={uid} status={status} dep={sumdep}")
    
    if uid:
        _stats["postbacks"] += 1
        if status == "ftd":
            _stats["ftd"] += 1
        asyncio.create_task(asyncio.to_thread(db_log_postback, uid, status, sumdep, country))
        ok = await asyncio.to_thread(db_save_trader, uid, sumdep, status, country)
        if not ok:
            _stats["save_fails"] += 1
            queue_failed_postback(uid, sumdep, status, country)
        # Auto send VIP if deposited
        if sumdep >= MIN_DEPOSIT:
            for chat_id, state in list(user_state.items()):
                if state.get("trader_id") == uid and state.get("step") != "done":
                    state["deposit"] = sumdep
                    state["step"] = "done"
                    try:
                        await tg_app.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"<b>{E_PARTY} Deposit Confirmed! WELCOME TO VIP! {E_CROWN}\n\n"
                                f"{E_FIRE} Join VIP:\n{VIP_LINK}\n\n"
                                f"{E_THUMBS} Welcome! {E_TROPHY}</b>"
                            ),
                            parse_mode=ParseMode.HTML,
                            reply_markup=vip_keyboard()
                        )
                    except Exception as e:
                        print(f"VIP send error: {e}")
                    break
    
    return web.Response(text="OK")

async def handle_addid(request):
    from aiohttp import web
    uid = request.rel_url.query.get("uid", "").strip()
    key = request.rel_url.query.get("key", "")
    if key != ADMIN_KEY:
        return web.Response(text="Forbidden", status=403)
    if uid:
        await asyncio.to_thread(db_save_trader, uid, 0.0, "manual", "")
        return web.Response(text=f"Added: {uid}")
    return web.Response(text="No uid")

async def handle_telegram(request):
    from aiohttp import web
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
    return web.Response(text="OK")


async def _on_error(update, context):
    """Catch any unhandled exception from a handler so one bad update can't
    crash the worker or flood the logs. Logs once, then moves on."""
    logging.getLogger("bot").error("Handler error: %s", context.error)

async def main():
    import telegram
    print(f"python-telegram-bot version: {telegram.__version__}")
    for _boot_try in range(5):
        try:
            _db_run("SELECT 1")
            break
        except Exception as e:
            print(f"DB not reachable yet (try {_boot_try + 1}/5): {e}")
            await asyncio.sleep(3)
    db_create_tables()
    global tg_app
    tg_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .connection_pool_size(100)
        .pool_timeout(30.0)
        .connect_timeout(20.0)
        .read_timeout(60.0)
        .write_timeout(60.0)
        .media_write_timeout(120.0)
        .rate_limiter(AIORateLimiter(overall_max_rate=25, overall_time_period=1, max_retries=5))
        .build()
    )
    tg_app.add_error_handler(_on_error)
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("clearreminders", clear_reminders))
    tg_app.add_handler(CommandHandler("stats", stats_command))
    for i in range(1, 6):
        tg_app.add_handler(CommandHandler(f"preview{i}", preview_reminder))
    tg_app.add_handler(CallbackQueryHandler(button_handler))
    tg_app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.VIDEO_NOTE | filters.Document.ALL | filters.Sticker.ALL, photo_handler))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    await tg_app.initialize()
    await tg_app.start()

    # Set webhook — use Railway's own public domain so this always points to THIS service.
    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if not public_domain:
        raise RuntimeError(
            "RAILWAY_PUBLIC_DOMAIN is not set. In Railway, open this service → "
            "Settings → Networking → Public Networking, and click 'Generate Domain'."
        )
    webhook_url = f"https://{public_domain}/telegram/{TOKEN}"
    await tg_app.bot.set_webhook(webhook_url, drop_pending_updates=False)
    print(f"✅ Webhook set on {public_domain}")
    asyncio.create_task(reminder_scheduler_loop(tg_app.bot))
    _pending_load()
    asyncio.create_task(postback_retry_loop())
    asyncio.create_task(daily_summary_loop(tg_app.bot))

    # Start web server
    from aiohttp import web
    app = web.Application()
    app.router.add_post(f"/telegram/{TOKEN}", handle_telegram)
    app.router.add_get("/postback", handle_postback)
    app.router.add_get("/addid", handle_addid)
    app.router.add_get("/", lambda r: web.Response(text="Trading Wolf Bot Running ✅"))

    async def handle_health(request):
        try:
            await asyncio.to_thread(_db_run, "SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        body = f'{{"ok": {str(db_ok).lower()}, "pending_postbacks": {len(_pending_postbacks)}}}'
        return web.Response(text=body, status=200 if db_ok else 503, content_type="application/json")
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))  # Railway provides PORT; fall back to 8080 locally
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Web server running on port {port}")
    print("✅ Trading Wolf Bot Running...")

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
