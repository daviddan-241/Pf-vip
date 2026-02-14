import asyncio
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ================= CONFIG =================
BOT_TOKEN = "7507047699:AAGJdALekJNNh1m2QBiQqLZS1kulS964DRw"

CHANNEL_ID = -1003461143473
VIP_GROUP_ID = -1003508806547
ADMIN_ID = 8297034218

BRAND_TAG = "\n\n📩 Promo / Listings / KOL collab:\n@edwardlucas09"
# ==========================================


# --------- COMMANDS ---------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 KOL bot is running.\nAdmin can use /post or /vippost."
    )


async def post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /post your message")
        return

    final_text = f"{text}{BRAND_TAG}"

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=final_text,
        disable_web_page_preview=False
    )

    await update.message.reply_text("✅ Posted to channel.")


async def vippost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: /vippost your VIP message")
        return

    vip_text = (
        "🔒 VIP ALERT\n\n"
        f"{text}"
        f"{BRAND_TAG}"
    )

    await context.bot.send_message(
        chat_id=VIP_GROUP_ID,
        text=vip_text,
        disable_web_page_preview=False
    )

    await update.message.reply_text("✅ Posted to VIP group.")


# --------- MAIN ---------

async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post", post))
    app.add_handler(CommandHandler("vippost", vippost))

    print("🤖 Bot started successfully")

    await app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    asyncio.run(main())