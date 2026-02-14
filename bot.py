import logging
import requests
import random
from datetime import datetime
from io import BytesIO
import matplotlib.pyplot as plt
from telegram import Update, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
BOT_TOKEN = "7507047699:AAGJdALekJNNh1m2QBiQqLZS1kulS964DRw"
ADMIN_ID = 8297034218
CHANNEL_ID = -1003461143473

COINGECKO_API = "https://api.coingecko.com/api/v3"
MIN_PROFIT_1H = 40.0
MIN_PROFIT_24H = 80.0
MIN_VOLUME_24H = 500000
CHECK_INTERVAL_MIN = 20

pending_posts = {}
posted_items = set()

PHRASE_VARIANTS = [
    "Strong momentum building – early positions look promising 🔥",
    "Pumping with conviction; volume supports further upside 📈",
    "Solid entry potential here; watch for continuation 🟢",
    "High-conviction play unfolding – consider scaling in 💎",
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Bot active.\n/start - this\n/status - jobs")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    jobs = context.job_queue.jobs()
    text = f"Active jobs: {len(jobs)}\n"
    for j in jobs:
        text += f"• {j.name} – next: {j.next_run_time}\n"
    await update.message.reply_text(text or "No jobs running.")

# ────────────────────────────────────────────────
def fetch_trending_coins():
    try:
        r = requests.get(f"{COINGECKO_API}/search/trending", timeout=12)
        coins = r.json().get("coins", [])[:15]

        hot = []
        for entry in coins:
            cid = entry["item"]["id"]
            if cid in posted_items: continue

            mk = requests.get(
                f"{COINGECKO_API}/coins/markets",
                params={"vs_currency": "usd", "ids": cid, "price_change_percentage": "1h,24h"},
                timeout=10
            ).json()
            if not mk: continue
            m = mk[0]

            ch1h = m.get("price_change_percentage_1h_in_currency", 0) or 0
            ch24h = m.get("price_change_percentage_24h_in_currency", 0) or 0
            vol = m.get("total_volume", 0)

            if (ch1h >= MIN_PROFIT_1H or ch24h >= MIN_PROFIT_24H) and vol >= MIN_VOLUME_24H:
                hot.append({
                    "type": "coin",
                    "id": cid,
                    "name": entry["item"]["name"],
                    "symbol": entry["item"]["symbol"].upper(),
                    "price": m["current_price"],
                    "change_1h": ch1h,
                    "change_24h": ch24h,
                    "market_cap": m.get("market_cap", 0),
                    "volume": vol,
                    "thumb": entry["item"].get("large") or entry["item"].get("thumb"),
                    "cg_link": f"https://www.coingecko.com/en/coins/{cid}"
                })
        return hot
    except Exception as e:
        logger.error(f"coins fetch failed: {e}")
        return []

def fetch_trending_nfts():
    try:
        r = requests.get(
            f"{COINGECKO_API}/nfts/markets",
            params={
                "vs_currency": "usd",
                "order": "volume_usd_24h_desc",
                "per_page": 50,
                "page": 1,
                "price_change_percentage": "1h,24h"
            },
            timeout=12
        )
        nfts = r.json()

        hot = []
        for nft in nfts:
            nid = nft["id"]
            if nid in posted_items: continue

            ch1h = nft.get("floor_price_percentage_change_1h_in_currency", 0) or 0
            ch24h = nft.get("floor_price_percentage_change_24h_in_currency", 0) or 0
            vol = nft.get("total_volume", 0)

            if (ch1h >= MIN_PROFIT_1H or ch24h >= MIN_PROFIT_24H) and vol >= MIN_VOLUME_24H:
                hot.append({
                    "type": "nft",
                    "id": nid,
                    "name": nft["name"],
                    "symbol": nft.get("symbol", "ETH").upper(),
                    "floor_price": nft.get("floor_price", 0),
                    "change_1h": ch1h,
                    "change_24h": ch24h,
                    "market_cap": nft.get("market_cap", 0),
                    "volume": vol,
                    "thumb": nft.get("image", {}).get("small"),
                    "cg_link": f"https://www.coingecko.com/en/nft/{nid}"
                })
        return hot
    except Exception as e:
        logger.error(f"nfts fetch failed: {e}")
        return []

# ────────────────────────────────────────────────
def generate_chart(item_id: str, item_type: str) -> BytesIO | None:
    try:
        base = "coins" if item_type == "coin" else "nfts"
        r = requests.get(
            f"{COINGECKO_API}/{base}/{item_id}/market_chart",
            params={"vs_currency": "usd", "days": "2", "interval": "hourly"},
            timeout=12
        )
        data = r.json()
        prices = data.get("prices", [])
        volumes = data.get("total_volumes", []) or [ [0,0] for _ in prices ]

        if len(prices) < 8:
            return None

        times = [datetime.fromtimestamp(ts/1000) for ts, _ in prices]
        pvals = [v for _, v in prices]
        vvals = [v for _, v in volumes]

        fig, ax1 = plt.subplots(figsize=(9, 5), facecolor="#0e111a")
        ax1.plot(times, pvals, color="#00e68a", lw=2.1, label="Price")
        ax1.set_facecolor("#0e111a")
        ax1.tick_params(colors="#cccccc")
        ax1.spines["bottom"].set_color("#444")
        ax1.spines["left"].set_color("#444")
        ax1.grid(True, alpha=0.12, color="#555")

        ax2 = ax1.twinx()
        ax2.bar(times, vvals, color="#444488", alpha=0.35, width=0.025, label="Volume")
        ax2.tick_params(colors="#cccccc")
        ax2.spines["right"].set_color("#444")

        fig.legend(loc="upper left", bbox_to_anchor=(0.12, 0.88), facecolor="#0e111a", edgecolor="#444", labelcolor="#eee")

        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.warning(f"chart failed: {e}")
        return None

# ────────────────────────────────────────────────
def build_post_text(item):
    ch1 = f"+{item['change_1h']:.1f}%" if item['change_1h'] > 0 else f"{item['change_1h']:.1f}%"
    ch24 = f"+{item['change_24h']:.1f}%" if item['change_24h'] > 0 else f"{item['change_24h']:.1f}%"
    cap = f"${item['market_cap']/1e6:,.1f}M" if item['market_cap'] else "—"
    vol = f"${item['volume']/1e6:,.1f}M" if item['volume'] else "—"

    phrase = random.choice(PHRASE_VARIANTS)

    if item["type"] == "coin":
        price_label = "Price"
        price_val = f"${item['price']:,.8f}" if item['price'] < 1 else f"${item['price']:,.4f}"
        symbol = f"${item['symbol']}"
    else:
        price_label = "Floor"
        price_val = f"Ξ {item['floor_price']:.3f} (${item['floor_price']*2500:,.0f})"  # rough ETH→USD
        symbol = item['symbol']

    text = f"""🟢 <b>{item['name']}  ({symbol}) – {'Token' if item['type']=='coin' else 'NFT'}</b>

💰 {price_label}: {price_val}
📈 1h: {ch1}   24h: {ch24}
🏦 MC: {cap}   Vol: {vol}

{phrase}

<a href="{item['cg_link']}">CoinGecko ↗</a>

→ Get your coin/NFT posted & promoted by KOLs: @edwardlucas09"""
    return text.strip()

# ────────────────────────────────────────────────
async def scan_and_preview(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Scanning trending assets...")
    items = fetch_trending_coins() + fetch_trending_nfts()
    if not items:
        return

    for item in items:
        chart_buf = generate_chart(item["id"], item["type"])
        text = build_post_text(item)

        media = []
        if chart_buf:
            media.append(InputMediaPhoto(media=chart_buf, caption=text, parse_mode="HTML"))
        if item.get("thumb"):
            media.append(InputMediaPhoto(media=item["thumb"]))

        if not media:
            continue

        sent_msgs = await context.bot.send_media_group(chat_id=ADMIN_ID, media=media)
        first_id = sent_msgs[0].message_id

        pending_posts[first_id] = {
            "item": item,
            "text": text,
            "file_ids": [m.photo[-1].file_id for m in sent_msgs if m.photo]
        }

        await context.bot.send_message(
            ADMIN_ID,
            f"New pump alert ↑\nReply with:\napprove {first_id}\nor approve all"
        )

# ────────────────────────────────────────────────
async def handle_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    txt = update.message.text.strip().lower()
    if not txt.startswith("approve"):
        return

    parts = txt.split()
    if len(parts) > 1 and parts[1] == "all":
        for mid in list(pending_posts):
            await post_to_channel(context, mid)
        await update.message.reply_text("Batch posted 🚀")
        return

    try:
        mid = int(parts[1]) if len(parts) > 1 else max(pending_posts.keys())
    except:
        await update.message.reply_text("Invalid ID")
        return

    if mid not in pending_posts:
        await update.message.reply_text("Post not found")
        return

    await post_to_channel(context, mid)
    await update.message.reply_text(f"Posted (ID {mid}) 🚀")

async def post_to_channel(context: ContextTypes.DEFAULT_TYPE, mid: int):
    data = pending_posts.pop(mid, None)
    if not data:
        return

    item = data["item"]
    posted_items.add(item["id"])

    media_group = [
        InputMediaPhoto(
            media=fid,
            caption=data["text"] if i == 0 else None,
            parse_mode="HTML"
        )
        for i, fid in enumerate(data["file_ids"])
    ]

    await context.bot.send_media_group(chat_id=CHANNEL_ID, media=media_group)

    try:
        await context.bot.delete_message(ADMIN_ID, mid)
    except:
        pass

# ────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_approval))

    app.job_queue.run_repeating(
        callback=scan_and_preview,
        interval=CHECK_INTERVAL_MIN * 60,
        first=30,
        name="scan_pumps"
    )

    logger.info("Bot starting – polling mode")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()