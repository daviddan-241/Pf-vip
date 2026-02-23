import os
import sys
import time
import random
import threading
import requests
from datetime import datetime
from io import BytesIO
import matplotlib.pyplot as plt
from filelock import FileLock
import telebot
from telebot import types
from flask import Flask, render_template_string
import atexit

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
BOT_TOKEN = "7507047699:AAGJdALekJNNh1m2QBiQqLZS1kulS964DRw"
ADMIN_ID = 8297034218
CHANNEL_ID = -1003461143473

COINGECKO_API = "https://api.coingecko.com/api/v3"
MIN_PROFIT_1H = 15.0     # was 40.0
MIN_PROFIT_24H = 30.0    # was 80.0
MIN_VOLUME_24H = 200000  # was 500000 – lower if you want smaller caps
CHECK_INTERVAL_MIN = 20

PHRASE_VARIANTS = [
    "Strong momentum building – early positions look promising 🔥",
    "Pumping with conviction; volume supports further upside 📈",
    "Solid entry potential here; watch for continuation 🟢",
    "High-conviction play unfolding – consider scaling in 💎",
]

bot = telebot.TeleBot(BOT_TOKEN)

pending_posts = {}      # message_id → {"text": ..., "photos": [file_ids]}
posted_items = set()    # avoid reposting same coin/nft id

# ────────────────────────────────────────────────
# FLASK KEEP-ALIVE SERVER
# ────────────────────────────────────────────────
flask_app = Flask(__name__)
PORT = int(os.environ.get('PORT', 10000))
start_time = time.time()

@flask_app.route('/')
def home():
    uptime = time.strftime('%H:%M:%S', time.gmtime(time.time() - start_time))
    return render_template_string('''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram KOL Bot</title>
        <meta http-equiv="refresh" content="300">
        <style>
            body { background:#0f0f23; color:#00ff9d; font-family:monospace; padding:40px; }
            h1 { color:#ffff00; }
            .box { background:#1a1a2e; padding:20px; border-radius:8px; margin:20px 0; }
        </style>
    </head>
    <body>
        <h1>🤖 KOL Pump Bot Status</h1>
        <div class="box">
            <p>✅ Running</p>
            <p>🕒 Uptime: {{ uptime }}</p>
            <p>Pending approvals: {{ pending }}</p>
        </div>
        <p>Auto-refresh every 5 min to help stay awake.</p>
    </body>
    </html>
    ''', uptime=uptime, pending=len(pending_posts))

@flask_app.route('/health')
def health():
    return {"status": "ok", "uptime": time.time() - start_time}, 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ────────────────────────────────────────────────
# SELF-PING THREAD
# ────────────────────────────────────────────────
def keep_alive_thread():
    while True:
        try:
            requests.get(f"http://localhost:{PORT}/health", timeout=8)
        except:
            pass
        time.sleep(240)

# ────────────────────────────────────────────────
# SINGLE INSTANCE LOCK
# ────────────────────────────────────────────────
lock_path = "/tmp/bot-run.lock"
lock = FileLock(lock_path)

try:
    lock.acquire(timeout=2)
except:
    print("Another instance is running → exiting")
    sys.exit(1)

def release_lock():
    try:
        lock.release()
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except:
        pass

atexit.register(release_lock)

# ────────────────────────────────────────────────
# DATA FETCH
# ────────────────────────────────────────────────
def fetch_trending_coins():
    try:
        r = requests.get(f"{COINGECKO_API}/search/trending", timeout=10)
        items = r.json().get("coins", [])[:12]
        hot = []
        for it in items:
            cid = it["item"]["id"]
            if cid in posted_items: continue
            mk = requests.get(f"{COINGECKO_API}/coins/markets?vs_currency=usd&ids={cid}&price_change_percentage=1h,24h", timeout=8).json()
            if not mk: continue
            m = mk[0]
            ch1 = m.get("price_change_percentage_1h_in_currency", 0) or 0
            ch24 = m.get("price_change_percentage_24h_in_currency", 0) or 0
            vol = m.get("total_volume", 0)
            if (ch1 >= MIN_PROFIT_1H or ch24 >= MIN_PROFIT_24H) and vol >= MIN_VOLUME_24H:
                hot.append({
                    "type": "coin", "id": cid, "name": it["item"]["name"],
                    "symbol": it["item"]["symbol"].upper(), "price": m["current_price"],
                    "change_1h": ch1, "change_24h": ch24,
                    "market_cap": m.get("market_cap", 0), "volume": vol,
                    "thumb": it["item"].get("large") or it["item"].get("thumb"),
                    "cg_link": f"https://www.coingecko.com/en/coins/{cid}"
                })
        return hot
    except:
        return []

def fetch_trending_nfts():
    try:
        r = requests.get(f"{COINGECKO_API}/nfts/markets?vs_currency=usd&order=volume_usd_24h_desc&per_page=40&price_change_percentage=1h,24h", timeout=10)
        nfts = r.json()
        hot = []
        for nft in nfts:
            nid = nft["id"]
            if nid in posted_items: continue
            ch1 = nft.get("floor_price_percentage_change_1h_in_currency", 0) or 0
            ch24 = nft.get("floor_price_percentage_change_24h_in_currency", 0) or 0
            vol = nft.get("total_volume", 0)
            if (ch1 >= MIN_PROFIT_1H or ch24 >= MIN_PROFIT_24H) and vol >= MIN_VOLUME_24H:
                hot.append({
                    "type": "nft", "id": nid, "name": nft["name"],
                    "symbol": nft.get("symbol", "ETH").upper(),
                    "floor_price": nft.get("floor_price", 0),
                    "change_1h": ch1, "change_24h": ch24,
                    "market_cap": nft.get("market_cap", 0), "volume": vol,
                    "thumb": nft.get("image", {}).get("small"),
                    "cg_link": f"https://www.coingecko.com/en/nft/{nid}"
                })
        return hot
    except:
        return []

# ────────────────────────────────────────────────
# CHART GENERATION (simple dark theme)
# ────────────────────────────────────────────────
def generate_chart(item_id, item_type):
    try:
        base = "coins" if item_type == "coin" else "nfts"
        r = requests.get(f"{COINGECKO_API}/{base}/{item_id}/market_chart?vs_currency=usd&days=2&interval=hourly", timeout=10)
        data = r.json()
        prices = data.get("prices", [])
        if len(prices) < 10: return None
        times = [datetime.fromtimestamp(ts/1000) for ts, _ in prices]
        vals = [v for _, v in prices]

        fig, ax = plt.subplots(figsize=(8, 4.5), facecolor="#0f0f17")
        ax.plot(times, vals, color="#00ff9d", lw=2)
        ax.set_facecolor("#0f0f17")
        ax.tick_params(colors="white")
        for s in ax.spines.values(): s.set_color("gray")
        ax.grid(alpha=0.15, color="gray")

        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
        buf.seek(0)
        plt.close(fig)
        return buf
    except:
        return None

# ────────────────────────────────────────────────
# POST TEXT BUILDER – with your requested @andrewcarlos09
# ────────────────────────────────────────────────
def build_post_text(item):
    ch1 = f"+{item['change_1h']:.1f}%" if item['change_1h'] > 0 else f"{item['change_1h']:.1f}%"
    ch24 = f"+{item['change_24h']:.1f}%" if item['change_24h'] > 0 else f"{item['change_24h']:.1f}%"
    cap = f"${item['market_cap']/1e6:.1f}M" if item['market_cap'] else "N/A"
    vol = f"${item['volume']/1e6:.1f}M" if item['volume'] else "N/A"
    phrase = random.choice(PHRASE_VARIANTS)

    if item["type"] == "coin":
        price_label = "Price"
        price_val = f"${item['price']:.8f}" if item['price'] < 1 else f"${item['price']:.4f}"
        symbol = f"${item['symbol']}"
    else:
        price_label = "Floor"
        price_val = f"≈ ${item['floor_price']*2400:,.0f}"  # rough ETH→USD estimate
        symbol = item['symbol']

    text = f"""
🟢 <b>{item['name']} ({symbol}) – { 'Token' if item['type']=='coin' else 'NFT' } Alert</b>

💰 {price_label}: {price_val}
📈 1h: {ch1}    24h: {ch24}
🏦 MC: {cap}     Vol: {vol}

{phrase}

<a href="{item['cg_link']}">View on CoinGecko</a>

→ Get your NFT/coin trending, posted & promoted by KOLs: @andrewcarlos09
    """.strip()
    return text

# ────────────────────────────────────────────────
# SCAN & SEND PREVIEW TO ADMIN
# ────────────────────────────────────────────────
def scan():
    items = fetch_trending_coins() + fetch_trending_nfts()
    if not items:
        return

    for item in items:
        chart = generate_chart(item["id"], item["type"])
        text = build_post_text(item)

        media = []
        if chart:
            media.append(types.InputMediaPhoto(chart, caption=text, parse_mode="HTML"))
        if item.get("thumb"):
            media.append(types.InputMediaPhoto(item["thumb"]))

        if not media:
            continue

        sent = bot.send_media_group(ADMIN_ID, media)
        msg_id = sent[0].message_id

        pending_posts[msg_id] = {
            "text": text,
            "file_ids": [m.photo[-1].file_id for m in sent if m.photo]
        }

        bot.send_message(ADMIN_ID, f"New alert ↑\nReply: approve {msg_id}\nor approve all")

# ────────────────────────────────────────────────
# APPROVAL HANDLING
# ────────────────────────────────────────────────
@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and m.text.lower().startswith("approve"))
def handle_approve(message):
    txt = message.text.lower().strip()
    parts = txt.split()

    if len(parts) > 1 and parts[1] == "all":
        for mid in list(pending_posts.keys()):
            post_to_channel(mid)
        bot.reply_to(message, "All posted 🚀")
        return

    try:
        mid = int(parts[1]) if len(parts) > 1 else max(pending_posts.keys() or [0])
    except:
        bot.reply_to(message, "Invalid format / no pending posts")
        return

    if mid not in pending_posts:
        bot.reply_to(message, "Post not found")
        return

    post_to_channel(mid)
    bot.reply_to(message, f"Posted! (ID {mid}) 🚀")

def post_to_channel(mid):
    data = pending_posts.pop(mid, None)
    if not data:
        return

    media = [types.InputMediaPhoto(fid, caption=data["text"] if i==0 else None, parse_mode="HTML")
             for i, fid in enumerate(data["file_ids"])]
    bot.send_media_group(CHANNEL_ID, media)

# ────────────────────────────────────────────────
# MAIN ENTRY POINT
# ────────────────────────────────────────────────
if __name__ == "__main__":
    print("Starting KOL bot + Flask keep-alive...")

    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive_thread, daemon=True).start()

    def scanner_loop():
        while True:
            try:
                scan()
            except Exception as e:
                print(f"Scan error: {e}")
            time.sleep(CHECK_INTERVAL_MIN * 60)

    threading.Thread(target=scanner_loop, daemon=True).start()

    print("Polling started")
    bot.infinity_polling(timeout=20, long_polling_timeout=15)