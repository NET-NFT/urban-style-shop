import json
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters
)

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://urban-style-shop.onrender.com")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(_name_)

user_carts = {}

try:
    with open("products.json", "r", encoding="utf-8") as f:
        PRODUCTS = json.load(f)
except Exception as e:
    logger.error(f"Ошибка загрузки products.json: {e}")
    PRODUCTS = []

# === Обработчики ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛍️ Добро пожаловать в *Urban Style*!\n\nВыберите категорию:",
        parse_mode="Markdown",
        reply_markup=category_menu()
    )

def category_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👕 Одежда", callback_data="cat_clothing")],
        [InlineKeyboardButton("👟 Обувь", callback_data="cat_shoes")],
        [InlineKeyboardButton("👜 Аксессуары", callback_data="cat_accessories")],
        [InlineKeyboardButton("🛒 Корзина", callback_data="cart")]
    ])

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "cat_clothing":
        await show_category(update, context, "clothing")
    elif data == "cat_shoes":
        await show_category(update, context, "shoes")
    elif data == "cat_accessories":
        await show_category(update, context, "accessories")
    elif data == "back_categories":
        await query.edit_message_text("Выберите категорию:", reply_markup=category_menu())
    elif data.startswith("view_"):
        prod_id = int(data.split("_")[1])
        await view_product(update, context, prod_id)
    elif data.startswith("add_"):
        prod_id = int(data.split("_")[1])
        if user_id not in user_carts:
            user_carts[user_id] = []
        user_carts[user_id].append(prod_id)
        await query.answer("✅ Добавлено!")
        await view_product(update, context, prod_id)
    elif data == "cart":
        await show_cart(update, context)
    elif data == "pay_rub":
        await send_rub_invoice(update, context)
    elif data.startswith("back_cat_"):
        category = data.split("_")[2]
        await show_category(update, context, category)

async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str):
    query = update.callback_query
    items = [p for p in PRODUCTS if p["category"] == category]
    if not items:
        await query.edit_message_text("В этой категории нет товаров.", reply_markup=back_kb())
        return

    buttons = [[InlineKeyboardButton(p["name"], callback_data=f"view_{p['id']}")] for p in items]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")])
    await query.edit_message_text("Выберите товар:", reply_markup=InlineKeyboardMarkup(buttons))

async def view_product(update: Update, context: ContextTypes.DEFAULT_TYPE, prod_id: int):
    query = update.callback_query
    product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
    if not product:
        await query.edit_message_text("Товар не найден.")
        return

    caption = f"*{product['name']}*\n\n{product['description']}\n\nЦена: {product['price_rub']} ₽"
    keyboard = [
        [InlineKeyboardButton("➕ В корзину", callback_data=f"add_{prod_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"back_cat_{product['category']}")]
    ]

    if product.get("photo_url"):
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(media=product["photo_url"], caption=caption, parse_mode="Markdown"),
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")]])

async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    cart = user_carts.get(user_id, [])
    if not cart:
        await query.edit_message_text("Корзина пуста.", reply_markup=back_kb())
        return

    total = 0
    items = {}
    for pid in cart:
        p = next(p for p in PRODUCTS if p["id"] == pid)
        key = (p["name"], p["price_rub"])
        items[key] = items.get(key, 0) + 1
        total += p["price_rub"]

    text = "🛒 *Ваша корзина:*\n\n"
    for (name, price), qty in items.items():
        text += f"- {name} × {qty}\n"
    text += f"\n*Итого: {total} ₽*"

    kb = [
        [InlineKeyboardButton("💳 Оплатить", callback_data="pay_rub")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")]
    ]
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def send_rub_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    cart = user_carts.get(user_id, [])
    if not cart:
        await query.edit_message_text("Корзина пуста.")
        return

    total_rub = sum(next(p["price_rub"] for p in PRODUCTS if p["id"] == pid) for pid in cart)
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title="Заказ в Urban Style",
        description="Оплата за выбранные товары",
        payload=f"order_{user_id}",
        provider_token=PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice("Общая сумма", total_rub * 100)],
        need_name=False,
        need_email=False,
        need_phone_number=False,
        need_shipping_address=False,
    )

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    payment = update.message.successful_payment
    user_id = user.id

    if user_id in user_carts:
        del user_carts[user_id]

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"✅ *Новый заказ!* \nПользователь: @{user.username}\nСумма: {payment.total_amount // 100} ₽",
        parse_mode="Markdown"
    )
    await update.message.reply_text("🎉 Спасибо за заказ! Менеджер свяжется с вами.")

from telegram.ext import Application, CommandHandler, CallbackQueryHandler, PreCheckoutQueryHandler, MessageHandler, filters
from flask import Flask, request
import os
import logging
import asyncio

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 10000))

# === Telegram Application ===
application = Application.builder().token(BOT_TOKEN).build()

# === Регистрация обработчиков ===
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

# === Flask-приложение ===
flask_app = Flask(__name__)

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    asyncio.run(application.process_update(update))
    return "ok"

if __name__ == "__main__":
    if WEBHOOK_URL:
        async def setup():
            await application.initialize()
            await application.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
            await application.start()
            logging.info(f"Webhook set to {WEBHOOK_URL}/{BOT_TOKEN}")
        asyncio.run(setup())
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        application.run_polling()
