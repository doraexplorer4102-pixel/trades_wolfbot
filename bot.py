import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for _noisy in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes,
)

# ── SECRETS ───────────────────────────────────────────────────────────────
def _require_env(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}.")
    return val

TOKEN = _require_env("BOT_TOKEN")

# ── THE ONLY EXTERNAL LINK ANYWHERE IN THIS BOT ──────────────────────────
# Telegram Ads policy: no gambling, no guaranteed-return claims, no broker
# or deposit links. This bot only ever links to your own public channel.
COMMUNITY_LINK = "https://t.me/+CTPtf1KYOghkNzdl"

# ── EDUCATIONAL TOPICS ────────────────────────────────────────────────────
# video_url is optional — leave as None until you have a real video/playlist
# link for that topic; the bot will just show the write-up until then.
TOPICS = {
    "candlestick": {
        "label": "📊 Candlestick Patterns",
        "video_url": None,
        "text": (
            "<b>📊 Candlestick Patterns</b>\n\n"
            "Candlestick charts show the open, high, low, and close price for "
            "a given period in a single visual shape. Learning to read them is "
            "one of the first steps in technical analysis.\n\n"
            "Common patterns worth studying:\n"
            "• Doji — indecision between buyers and sellers\n"
            "• Hammer / Shooting Star — potential reversal signals\n"
            "• Engulfing patterns — momentum shifting from one side to the other\n\n"
            "These patterns describe price behaviour — they are observations, "
            "not predictions or guarantees of what will happen next."
        ),
    },
    "chart_patterns": {
        "label": "📈 Chart Patterns",
        "video_url": None,
        "text": (
            "<b>📈 Chart Patterns</b>\n\n"
            "Chart patterns form over many candles and can hint at how supply "
            "and demand are balanced in a market.\n\n"
            "Some classic patterns to learn:\n"
            "• Support & Resistance — price levels that have mattered before\n"
            "• Triangles & Flags — periods of consolidation\n"
            "• Head & Shoulders — a well-known potential reversal shape\n\n"
            "Like all technical analysis, these are tools for reading context — "
            "not a promise of future price movement."
        ),
    },
    "risk_management": {
        "label": "🛡️ Risk Management",
        "video_url": None,
        "text": (
            "<b>🛡️ Risk Management</b>\n\n"
            "Protecting your capital matters more than any single trade.\n\n"
            "Core ideas to learn:\n"
            "• Position sizing — never risk more than a small % of your capital "
            "on one trade\n"
            "• Stop-loss orders — decide your exit before you enter\n"
            "• Risk-to-reward ratio — know what you're risking vs. what you "
            "could gain\n\n"
            "No system removes risk entirely. Managing it well is what "
            "separates a disciplined approach from a careless one."
        ),
    },
    "money_management": {
        "label": "💰 Money Management",
        "video_url": "https://youtu.be/d1RBcGnh8ro?si=n5v9zxHo7bticm4l",
        "text": (
            "<b>💰 Money Management</b>\n\n"
            "This is about how you handle your capital over time, not just in "
            "a single trade.\n\n"
            "Ideas worth understanding:\n"
            "• Only trade with money you can afford to lose\n"
            "• Avoid overleveraging your account\n"
            "• Track your trades so you can learn from real data, not memory\n\n"
            "There are no shortcuts here — consistent habits matter more than "
            "any single decision."
        ),
    },
    "basics": {
        "label": "📚 Trading Basics",
        "video_url": None,
        "text": (
            "<b>📚 Trading Basics (Beginner)</b>\n\n"
            "If you're just starting out, begin here:\n\n"
            "• What a market actually is, and how buying/selling works\n"
            "• Reading a basic price chart\n"
            "• Common terms: bid, ask, spread, volume, timeframe\n\n"
            "Take your time with the basics — a solid foundation makes "
            "everything after this easier to understand."
        ),
    },
    "advanced": {
        "label": "🎓 Advanced Concepts",
        "video_url": "https://youtu.be/o-9N7iS_jVQ?si=9a21k2zIm3KfaXLf",
        "text": (
            "<b>🎓 Advanced Trading Concepts</b>\n\n"
            "Once the basics feel comfortable, these are worth exploring:\n\n"
            "• Technical indicators (moving averages, RSI, MACD) and what they "
            "actually measure\n"
            "• Multiple timeframe analysis\n"
            "• How different indicators can confirm — or contradict — each other\n\n"
            "These are analytical tools to support your own thinking, not "
            "signals to follow blindly."
        ),
    },
    "psychology": {
        "label": "🧠 Trading Psychology",
        "video_url": "https://youtu.be/-Jx0PXkpiLw?si=UGN3fRG1_hw_jt2E",
        "text": (
            "<b>🧠 Trading Psychology</b>\n\n"
            "Often the hardest part of trading has nothing to do with charts.\n\n"
            "Worth reflecting on:\n"
            "• Why revenge trading after a loss usually makes things worse\n"
            "• Sticking to a plan instead of trading on emotion\n"
            "• Keeping a journal to notice your own patterns over time\n\n"
            "Discipline and patience are skills — they're built gradually, "
            "not overnight."
        ),
    },
    "fake_breakouts": {
        "label": "🔎 Identifying Fake Breakouts",
        "video_url": "https://youtu.be/8EMxQCX5xpA?si=cMnAOpJ22K3pvdD4",
        "text": (
            "<b>🔎 Identifying Fake Breakouts</b>\n\n"
            "A breakout happens when price moves beyond a known support or "
            "resistance level. Not every breakout continues, though — a "
            "\"fake breakout\" is when price pushes through a level briefly "
            "and then reverses back.\n\n"
            "Learning to recognise the difference is a key chart-reading "
            "skill, and this video walks through it in detail."
        ),
    },
    "support_resistance": {
        "label": "📐 Support & Resistance",
        "video_url": "https://youtu.be/9VZnZ_UzOIU?si=9UnxLtD8bXUxhpgW",
        "text": (
            "<b>📐 Master Support & Resistance</b>\n\n"
            "Support and resistance levels are some of the most fundamental "
            "concepts in chart reading — price levels where buying or "
            "selling pressure has historically shown up.\n\n"
            "This crash course covers how to spot them and why they matter."
        ),
    },
    "indicator_basics": {
        "label": "🧮 Indicator Strategy Basics",
        "video_url": "https://youtu.be/kiAmW9WQ5hU?si=DoH5eBB9wDT94JUG",
        "text": (
            "<b>🧮 Indicator Strategy for Beginners</b>\n\n"
            "Technical indicators can help add structure to how you read a "
            "chart. This video walks through a beginner-friendly approach "
            "to using them as part of your own analysis."
        ),
    },
}

TOPIC_ORDER = [
    "candlestick", "chart_patterns", "risk_management", "money_management",
    "basics", "advanced", "psychology",
    "fake_breakouts", "support_resistance", "indicator_basics",
]


def main_menu_keyboard():
    rows = []
    row = []
    for key in TOPIC_ORDER:
        row.append(InlineKeyboardButton(TOPICS[key]["label"], callback_data=f"topic:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("👥 Join Our Community", url=COMMUNITY_LINK)])
    return InlineKeyboardMarkup(rows)


def topic_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Back to Topics", callback_data="back_to_menu")],
        [InlineKeyboardButton("👥 Join Our Community", url=COMMUNITY_LINK)],
    ])


WELCOME_TEXT = (
    "<b>Welcome!</b>\n\n"
    "This bot is here to help you learn trading concepts — patterns, risk "
    "management, and the fundamentals — step by step.\n\n"
    "Pick a topic below to get started:"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard()
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_to_menu":
        await query.edit_message_text(
            WELCOME_TEXT, parse_mode="HTML", reply_markup=main_menu_keyboard()
        )
        return

    if data.startswith("topic:"):
        key = data.split(":", 1)[1]
        topic = TOPICS.get(key)
        if not topic:
            return
        text = topic["text"]
        if topic.get("video_url"):
            text += f"\n\n📺 Watch: {topic['video_url']}"
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=topic_back_keyboard()
        )


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    logging.getLogger("wolf_edu_bot").info("Trading Wolf (educational) bot started — polling.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
