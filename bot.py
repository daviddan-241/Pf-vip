import os
import logging
import requests
from datetime import datetime, timedelta
from io import BytesIO
import matplotlib.pyplot as plt
from telegram import Update, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackContext,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
BOT_TOKEN = "7507047699:AAGJdALekJNNh1m2QBiQqLZS1kulS964DRw"
ADMIN_ID = 8297034218
CHANNEL_ID = -1003461143473

COINGECKO_API = "https://api.coingecko.com/api/v3"
MIN_PROFIT_1H = 40.0      # % pump in 1h to consider "hot"
MIN_PROFIT_24H = 80.0     # or big 24h move
CHECK_INTERVAL_MIN = 20   # how often to scan

pending_posts = {}  # msg_id_in_chat → formatted text + image bytes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Admin only.")
        return
    await update.message.reply_text(
        "Bot running.\nCommands:\n/start - this\n/status - check scheduler"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    jobs = context.job_queue.get_jobs_by_name("scan_trending")
    text = f"Active scanners: {len(jobs)}\nNext run: {jobs[0].next_t if jobs else 'N/A'}"
    await update.message.reply_text(text)

# ────────────────────────────────────────────────
def fetch_trending_coins():
    try:
        # 1. Get trending search
        r = requests.get(f"{COINGECKO_API}/search/trending", timeout=10)
        data = r.json()
        trending = data.get("coins", [])[:12]  # top 12

        hot = []
        for item in trending:
            coin = item["item"]
            id_ = coin["id"]
            symbol = coin["symbol"].upper()

            # 2. Get market data
            m = requests.get(
                f"{COINGECKO_API}/coins/markets",
                params={"vs_currency": "usd", "ids": id_, "price_change_percentage": "1h,24h"},
                timeout=8
            ).json()

            if not m: continue
            m = m[0]

            change_1h = m.get("price_change_percentage_1h_in_currency", 0)
            change_24h = m.get("price_change_percentage_24h_in_currency", 0)

            if change_1h >= MIN_PROFIT_1H or change_24h >= MIN_PROFIT_24H:
                hot.append({
                    "name": coin["name"],
                    "symbol": symbol,
                    "price": m["current_price"],
                    "change_1h": change_1h,
                    "change_24h": change_24h,
                    "market_cap": m.get("market_cap", 0),
                    "volume": m.get("total_volume", 0),
                    "thumb": coin.get("thumb"),
                    "id": id_
                })
        return hot
    except Exception as e:
        logger.error(f"fetch_trending error: {e}")
        return []

# For NFTs you can do similar with /nfts/markets and filter high 24h volume change

# ────────────────────────────────────────────────
def generate_chart(coin_id: str) -> BytesIO | None:
    try:
        # Last 2 days hourly prices
        url = f"{COINGECKO_API}/coins/{coin_id}/market_chart"
        params = {"vs_currency": "usd", "days": "2", "interval": "hourly"}
        data = requests.get(url, params=params, timeout=10).json()
        prices = data.get("prices", [])
        if len(prices) < 10: return None

        times = [datetime.fromtimestamp(p[0]/1000) for p in prices]
        vals = [p[1] for p in prices]

        fig, ax = plt.subplots(figsize=(8, 4.5), facecolor="#0f0f17")
        ax.plot(times, vals, color="#00ff9d", linewidth=2.2)
        ax.set_facecolor("#0f0f17")
        ax.tick_params(colors="white")
        for spine in ax.spines.values(): spine.set_color("gray")
        ax.grid(alpha=0.15, color="gray")

        buf = BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=120, facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except:
        return None

# ────────────────────────────────────────────────
def build_post_text(item):
    ch1 = f"+{item['change_1h']:.1f}%" if item['change_1h'] > 0 else f"{item['change_1h']:.1f}%"
    ch24 = f"+{item['change_24h']:.1f}%" if item['change_24h'] > 0 else f"{item['change_24h']:.1f}%"
    cap = f"${item['market_cap']/1e6:.1f}M" if item['market_cap'] else "N/A"
    vol = f"${item['volume']/1e6:.1f}M" if item['volume'] else "N/A"

    text = f"""
🟢 <b>{item['name']} (${item['symbol']}) pumping hard</b>

💰 Price: ${item['price']:.6f}
📈 1h: {ch1}    24h: {ch24}
🏦 MC: {cap}     Vol: {vol}

Very strong momentum – early entry still looks juicy 🔥

→ To get your coin posted, listed & shilled by real KOLs:
DM @edwardlucas09 right now
    """.strip()
    return text

# ────────────────────────────────────────────────
async def scan_and_preview(context: CallbackContext):
    logger.info("Scanning trending coins...")
    hot_coins = fetch_trending_coins()

    for coin in hot_coins:
        chart_buf = generate_chart(coin["id"])
        thumb_url = coin.get("thumb")

        text = build_post_text(coin)

        media = []
        if chart_buf:
            media.append(InputMediaPhoto(media=chart_buf, caption=text, parse_mode="HTML"))
        if thumb_url:
            media.append(InputMediaPhoto(media=thumb_url))

        if not media:
            media = [InputMediaPhoto(media="https://via.placeholder.com/512x512/0f0f17/00ff9d?text="+coin['symbol'], caption=text, parse_mode="HTML")]

        # Send preview to admin (private)
        sent = await context.bot.send_media_group(
            chat_id=ADMIN_ID,
            media=media
        )

        # Remember first message id for approval
        pending_posts[sent[0].message_id] = {
            "text": text,
            "images": [m.message_id for m in sent]  # to forward later
        }

        await context.bot.send_message(
            ADMIN_ID,
            f"↑ Approve this post? Reply with <code>approve {sent[0].message_id}</code>\nor just <code>approve</code> for the last one.",
            parse_mode="HTML"
        )

# ────────────────────────────────────────────────
async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = update.message.text.strip().lower()
    if not text.startswith("approve"):
        return

    parts = text.split()
    if len(parts) > 1 and parts[1].isdigit():
        msg_id = int(parts[1])
    else:
        # last pending
        if not pending_posts:
            await update.message.reply_text("No pending posts.")
            return
        msg_id = list(pending_posts.keys())[-1]

    if msg_id not in pending_posts:
        await update.message.reply_text("Post not found or already cleared.")
        return

    data = pending_posts.pop(msg_id)

    # Forward the media group to channel (or re-send)
    await context.bot.send_media_group(
        chat_id=CHANNEL_ID,
        media=[
            InputMediaPhoto(media=m) for m in data["images"]  # simplistic; real case download + reupload if needed
        ]
    )

    # Optional: delete preview from admin chat
    try:
        await context.bot.delete_message(ADMIN_ID, msg_id)
    except:
        pass

    await update.message.reply_text(f"Posted! 🚀 (msg {msg_id})")

# ────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_approval))

    # Schedule scanner
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_and_preview,
        "interval",
        minutes=CHECK_INTERVAL_MIN,
        args=(None,),  # dummy for job
        name="scan_trending",
        id="scan_trending"
    )
    scheduler.start()

    # Optional: run once on start
    app.job_queue.run_once(scan_and_preview, 10, name="initial_scan")

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()