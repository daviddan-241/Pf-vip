import os, requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, ContextTypes
)

# ================= CONFIG =================
BOT_TOKEN = "7507047699:AAGJdALekJNNh1m2QBiQqLZS1kulS964DRw"

ADMIN_ID = 8297034218
CHANNEL_ID = -1003461143473
VIP_GROUP_ID = -1003508806547

BRAND = "\n\n📩 Promo & Collabs: @edwardlucas09"
VIP_ADDRESS = "EaFeqxptPuo2jy3dA8dRsgRz8JRCPSK5mXT3qZZYT7f3"

VIP_TIERS = {"weekly": 7, "monthly": 30, "lifetime": 3650}

vip_users = {}            # username -> expiry
reminded = set()
pending_posts = []        # queued posts
pending_payments = {}     # payment claims

# ================= HELPERS =================
def chart_image():
    return "https://dexscreener.com/solana?embed=1&theme=dark"

def live_stats():
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/search?q=solana",
            timeout=10
        ).json()
        p = r["pairs"][0]
        mc = int(p.get("fdv") or 0)
        vol = int(p.get("volume", {}).get("h24", 0))
        ca = p["baseToken"]["address"]
        short_ca = ca[:4] + "..." + ca[-4:]
        score = (vol // 10000) + (mc // 100000)
        return mc, vol, short_ca, score
    except:
        return 0, 0, "N/A", 0

# ================= POST BUILDERS =================
def build_coin_posts():
    mc, vol, ca, score = live_stats()

    # PUBLIC CHANNEL (TEASER)
    public = f"""
🚀 NEW PUMP.FUN TREND

MC: ${mc:,}
24H Volume: ${vol:,}

Solana meme gaining attention
Momentum starting to build

CA:
{ca}

⚠️ High risk | Fast moves

🔒 Full breakdown in VIP
💰 Pay (SOL):
{VIP_ADDRESS}
""".strip() + BRAND

    # VIP GROUP (FULL ALPHA)
    vip = f"""
🚀 VIP PUMP.FUN CALL

MC: ${mc:,}
24H Volume: ${vol:,}

CA:
{ca}

🧠 Wallet Flow:
• Fresh wallets rotating in
• No heavy distribution yet
• Early discovery phase

🎯 Execution Plan:
• Starter size only
• Add on volume confirmation
• Trim into strength

⚠️ Not financial advice
""".strip() + BRAND

    return public, vip, score

def build_nft_posts():
    # PUBLIC CHANNEL
    public = f"""
🖼️ NFT MARKET ACTIVITY

NFT collection starting to trend
Collectors watching closely

🔒 Full NFT alpha in VIP
💰 Pay (SOL):
{VIP_ADDRESS}
""".strip() + BRAND

    # VIP GROUP
    vip = """
🖼️ VIP NFT ALPHA

🧠 Smart money signals:
• Floor stabilizing
• Listings thinning
• Early hype stage

🎯 Play:
• Scale in early
• Flip on volume expansion

⚠️ NFTs are high risk
""".strip() + BRAND

    return public, vip, 5

# ================= AUTO QUEUE =================
async def queue_coins(context):
    pub, vip, score = build_coin_posts()
    pending_posts.append(("coin", pub, vip, score))

async def queue_nfts(context):
    pub, vip, score = build_nft_posts()
    pending_posts.append(("nft", pub, vip, score))

# ================= REVIEW =================
async def review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not pending_posts:
        await update.message.reply_text("✅ No pending posts.")
        return

    kind, pub, vip, score = pending_posts[0]

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data="approve"),
        InlineKeyboardButton("❌ Reject", callback_data="reject")
    ]])

    await update.message.reply_photo(
        photo=chart_image(),
        caption="📋 VIP PREVIEW\n\n" + vip,
        reply_markup=kb
    )

async def approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not pending_posts:
        return

    kind, pub, vip, score = pending_posts.pop(0)

    if q.data == "approve":
        pub_msg = await context.bot.send_photo(
            CHANNEL_ID, chart_image(), caption=pub
        )
        vip_msg = await context.bot.send_photo(
            VIP_GROUP_ID, chart_image(), caption=vip
        )

        # 🎯 Auto pin best VIP calls
        if score >= 10:
            try:
                await context.bot.pin_chat_message(
                    VIP_GROUP_ID, vip_msg.message_id
                )
            except:
                pass

        await q.edit_message_caption("✅ Posted (VIP best calls pinned)")
    else:
        await q.edit_message_caption("❌ Rejected")

# ================= VIP FLOW =================
async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💳 I PAID", callback_data="ipaid")
    ]])

    await update.message.reply_text(
        f"""🔒 VIP ACCESS

Weekly – 7 days
Monthly – 30 days
Lifetime – Unlimited

💰 SOL Address:
{VIP_ADDRESS}

After payment, tap **I PAID**.
""",
        reply_markup=kb
    )

async def ipaid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user.username
    if not user:
        await q.edit_message_text("❌ Set a Telegram username first.")
        return

    admin_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"pay_ok:{user}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"pay_no:{user}")
    ]])

    await context.bot.send_message(
        ADMIN_ID,
        f"💳 PAYMENT CLAIM\nUser: @{user}",
        reply_markup=admin_kb
    )

    await q.edit_message_text("⏳ Payment submitted for review.")

async def payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    action, user = q.data.split(":")

    if action == "pay_ok":
        expiry = datetime.utcnow() + timedelta(days=VIP_TIERS["monthly"])
        vip_users[user] = expiry

        invite = await context.bot.create_chat_invite_link(
            VIP_GROUP_ID, member_limit=1
        )

        await context.bot.send_message(
            f"@{user}",
            f"🎉 VIP APPROVED\n\nJoin VIP:\n{invite.invite_link}"
        )
        await q.edit_message_text(f"✅ Approved @{user}")

    else:
        await context.bot.send_message(
            f"@{user}",
            "❌ Payment not found. Please try again."
        )
        await q.edit_message_text(f"❌ Rejected @{user}")

# ================= DASHBOARD =================
async def viplist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not vip_users:
        await update.message.reply_text("No VIP members.")
        return

    msg = "👑 VIP DASHBOARD\n\n"
    for u, e in vip_users.items():
        msg += f"@{u} → {e.strftime('%Y-%m-%d')}\n"
    await update.message.reply_text(msg)

# ================= REMINDERS =================
async def reminders(context):
    now = datetime.utcnow()
    for u, e in vip_users.items():
        if (e - now).days == 3 and u not in reminded:
            try:
                await context.bot.send_message(
                    f"@{u}",
                    f"⏳ VIP expires in 3 days.\n"
                    f"Renew to keep access.\n\n💰 {VIP_ADDRESS}"
                )
                reminded.add(u)
            except:
                pass

# ================= AUTO DM UPSELL =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"""👋 Welcome

I post **Pump.fun coins**, **wallet-flow plays**, and **NFT alpha**.

🔒 VIP members get:
• Full breakdowns
• Best calls pinned
• Private NFT plays

💰 Pay (SOL):
{VIP_ADDRESS}

Tap /vip to upgrade.
"""
    )

# ================= CLEANUP =================
async def cleanup(context):
    now = datetime.utcnow()
    expired = [u for u, e in vip_users.items() if now > e]
    for u in expired:
        vip_users.pop(u, None)

# ================= MAIN =================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vip", vip))
    app.add_handler(CommandHandler("viplist", viplist))
    app.add_handler(CommandHandler("review", review))

    app.add_handler(CallbackQueryHandler(ipaid, pattern="^ipaid$"))
    app.add_handler(CallbackQueryHandler(payment_decision, pattern="^pay_"))
    app.add_handler(CallbackQueryHandler(approval, pattern="^(approve|reject)$"))

    app.job_queue.run_repeating(queue_coins, 7200)
    app.job_queue.run_repeating(queue_nfts, 21600)
    app.job_queue.run_repeating(reminders, 86400)
    app.job_queue.run_repeating(cleanup, 3600)

    app.run_polling(
    allowed_updates=Update.ALL_TYPES,
    close_loop=False
)

if __name__ == "__main__":
    main()
