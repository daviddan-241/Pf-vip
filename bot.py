from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

BOT_TOKEN = "7507047699:AAGJdALekJNNh1m2QBiQqLZS1kulS964DRw"

CHANNEL_ID = -1003461143473
VIP_GROUP_ID = -1003508806547
ADMIN_ID = 8297034218

BRAND = "\n\n📩 Promo / Listings / KOL collab:\n@edwardlucas09"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 KOL bot is running.")


async def post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Use: /post message")
        return
    await context.bot.send_message(CHANNEL_ID, text + BRAND)


async def vippost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Use: /vippost message")
        return
    await context.bot.send_message(
        VIP_GROUP_ID,
        "🔒 VIP ALERT\n\n" + text + BRAND
    )


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("post", post))
    app.add_handler(CommandHandler("vippost", vippost))

    print("✅ Bot started cleanly")
    app.run_polling()


if __name__ == "__main__":
    main()