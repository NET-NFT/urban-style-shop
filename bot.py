import json
import os
import logging
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputMediaPhoto
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # Убираем пробелы

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Хранение данных ===
user_carts = {}
games = {}  # Для крестиков-ноликов
active_games = {}      # Игры между двумя игроками
pending_invites = {}   # Ожидающие приглашения

# === Загрузка товаров ===
try:
    with open("products.json", "r", encoding="utf-8") as f:
        PRODUCTS = json.load(f)
except Exception as e:
    logger.error(f"Ошибка загрузки products.json: {e}")
    PRODUCTS = []

# === Вспомогательные функции для игры ===
def create_game_board():
    return [" " for _ in range(9)]

def check_win(board, player):
    win_conditions = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6)
    ]
    return any(all(board[i] == player for i in cond) for cond in win_conditions)

def check_draw(board):
    return " " not in board

def get_game_keyboard(board):
    keyboard = []
    for row in range(3):
        buttons = []
        for col in range(3):
            idx = row * 3 + col
            text = board[idx] if board[idx] != " " else " "
            callback = f"move_{idx}" if board[idx] == " " else "ignore"
            buttons.append(InlineKeyboardButton(text, callback_data=callback))
        keyboard.append(buttons)
    return InlineKeyboardMarkup(keyboard)

def generate_promo():
    return "WIN" + str(random.randint(1000, 9999))

# === Обработчики магазина ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].startswith("ttt_"):
        game_id = context.args[0][4:]
        await join_ttt_game(update, context, game_id)
    else:
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
        [InlineKeyboardButton("🛒 Корзина", callback_data="cart")],
        [InlineKeyboardButton("🎮 Крестики-нолики", callback_data="ttt_menu")],
         [InlineKeyboardButton("↓ Игры 🎮 ↓")]
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
    elif data == "ttt_game":
        await query.answer()
        await start_ttt(update, context)
    elif data == "ttt_menu":
        await ttt_menu(update, context)
    elif data == "ttt_vs_bot":
        await start_ttt(update, context)
    elif data == "ttt_vs_friend":
        await create_ttt_game(update, context)

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

# === Обработчики игры ===
async def start_ttt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запуск игры с ботом")
    chat_id = update.effective_chat.id
    board = create_game_board()
    games[chat_id] = {
        'board': board,
        'current_player': 'X',
        'vs_bot': True  # ← игра против бота
    }
    await context.bot.send_message(
        chat_id=chat_id,
        text="🎮 Игра против бота!\nВы — X. Сделайте свой ход:",
        reply_markup=get_game_keyboard(board)
    )
    
async def ttt_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("ttt_menu вызван")
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Выберите режим:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("С ботом", callback_data="ttt_vs_bot")],
            [InlineKeyboardButton("С другом", callback_data="ttt_vs_friend")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")]
        ])
    )

async def ttt_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    move_index = int(query.data.split('_')[1])

    # Сначала проверяем мультиплеер
    game_id = None
    game = None
    for gid, g in active_games.items():
        if user_id in (g['player_x_id'], g['player_o_id']):
            game_id = gid
            game = g
            break

    if game:
        # Мультиплеерная игра
        if game['current_turn'] != user_id:
            symbol = "X" if user_id == game['player_x_id'] else "O"
            await query.answer(f"Сейчас ход противника! Вы — {symbol}.")
            return

        board = game['board']
        if board[move_index] != " ":
            await query.answer("Ячейка занята!")
            return

        player_symbol = "X" if user_id == game['player_x_id'] else "O"
        board[move_index] = player_symbol

        if check_win(board, player_symbol):
            promo = generate_promo() if player_symbol == "X" else "Попробуй ещё раз!"
            winner_name = "Вы" if user_id == game['player_x_id'] else "Ваш друг"
            result_text = f"🎉 {winner_name} победил как {player_symbol}!\n\n"
            if player_symbol == "X":
                result_text += f"Твой промокод: `{promo}`\n+30 ⭐️ бонусов!"
            
            await context.bot.edit_message_text(
                chat_id=game['chat_id_x'],
                message_id=game['msg_id_x'],
                text=result_text,
                parse_mode="Markdown" if player_symbol == "X" else None
            )
            await context.bot.edit_message_text(
                chat_id=game['chat_id_o'],
                message_id=game['msg_id_o'],
                text=result_text,
                parse_mode="Markdown" if player_symbol == "X" else None
            )
            del active_games[game_id]
            return

        if check_draw(board):
            await context.bot.edit_message_text(
                chat_id=game['chat_id_x'],
                message_id=game['msg_id_x'],
                text="🤝 Ничья!"
            )
            await context.bot.edit_message_text(
                chat_id=game['chat_id_o'],
                message_id=game['msg_id_o'],
                text="🤝 Ничья!"
            )
            del active_games[game_id]
            return

        next_player = game['player_o_id'] if user_id == game['player_x_id'] else game['player_x_id']
        game['current_turn'] = next_player

        next_symbol = "O" if player_symbol == "X" else "X"
        await context.bot.edit_message_text(
            chat_id=game['chat_id_x'],
            message_id=game['msg_id_x'],
            text=f"Ходит {'O' if user_id == game['player_x_id'] else 'X'} ({next_symbol}):",
            reply_markup=get_game_keyboard(board)
        )
        await context.bot.edit_message_text(
            chat_id=game['chat_id_o'],
            message_id=game['msg_id_o'],
            text=f"Ходит {'O' if user_id == game['player_x_id'] else 'X'} ({next_symbol}):",
            reply_markup=get_game_keyboard(board)
        )
        return

    # Если не мультиплеер — игра с ботом (старая логика)
    chat_id = query.message.chat.id
    if chat_id not in games:
        await context.bot.send_message(chat_id=chat_id, text="Игра не найдена.")
        return

    game_bot = games[chat_id]
    board = game_bot['board']
    if board[move_index] != " ":
        await query.answer("Эта ячейка уже занята!")
        return

    board[move_index] = 'X'

    if check_win(board, 'X'):
        promo = generate_promo()
        result_text = f"🎉 Вы победили! 🎉\n\nТвой промокод: `{promo}`\n+30 ⭐️ бонусов на счёт!"
        del games[chat_id]
        await query.edit_message_text(
            text=result_text,
            reply_markup=None,
            parse_mode="Markdown"
        )
        return

    if check_draw(board):
        result_text = "🤝 Ничья! 🤝"
        del games[chat_id]
        await query.edit_message_text(text=result_text, reply_markup=None)
        return

    empty_cells = [i for i, cell in enumerate(board) if cell == " "]
    if empty_cells:
        bot_move = random.choice(empty_cells)
        board[bot_move] = 'O'

        if check_win(board, 'O'):
            result_text = "🤖 Бот победил! Попробуй ещё раз!"
            del games[chat_id]
            await query.edit_message_text(text=result_text, reply_markup=None)
            return

        if check_draw(board):
            result_text = "🤝 Ничья! 🤝"
            del games[chat_id]
            await query.edit_message_text(text=result_text, reply_markup=None)
            return

    await query.edit_message_text(
        text="Ваш ход:",
        reply_markup=get_game_keyboard(board)
    )

import uuid

async def create_ttt_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    game_id = str(uuid.uuid4())[:8]
    
    pending_invites[game_id] = {
        'creator_id': user.id,
        'creator_name': user.first_name,
        'chat_id': update.effective_chat.id
    }
    
    bot_username = context.bot.username
    invite_link = f"https://t.me/{bot_username}?start=ttt_{game_id}"
    
    await update.effective_message.reply_text(
        f"🎮 Игра создана!\n\nОтправьте эту ссылку другу:\n\n`{invite_link}`",
        parse_mode="Markdown"
    )

async def join_ttt_game(update: Update, context: ContextTypes.DEFAULT_TYPE, game_id: str):
    user = update.effective_user
    chat_id = update.effective_chat.id

    if game_id not in pending_invites:
        await update.message.reply_text("❌ Игра не найдена или уже началась.")
        return

    invite = pending_invites[game_id]
    if invite['creator_id'] == user.id:
        await update.message.reply_text("Вы уже создали эту игру!")
        return

    board = create_game_board()
    active_games[game_id] = {
        'board': board,
        'player_x_id': invite['creator_id'],
        'player_o_id': user.id,
        'current_turn': invite['creator_id'],
        'chat_id_x': invite['chat_id'],
        'chat_id_o': chat_id
    }

    del pending_invites[game_id]

    keyboard = get_game_keyboard(board)
    msg_x = await context.bot.send_message(
        chat_id=invite['chat_id'],
        text=f"✅ {user.first_name} присоединился!\n\nВаш ход (X):",
        reply_markup=keyboard
    )
    msg_o = await context.bot.send_message(
        chat_id=chat_id,
        text=f"Вы играете за O.\n\nХодит {invite['creator_name']} (X)...",
        reply_markup=keyboard
    )

    active_games[game_id]['msg_id_x'] = msg_x.message_id
    active_games[game_id]['msg_id_o'] = msg_o.message_id

# === Запуск ===
if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tictactoe", start_ttt))
    app.add_handler(CallbackQueryHandler(ttt_move, pattern="^move_"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^ignore$"))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(cat_|cart|ttt_game|ttt_menu|ttt_vs_bot|ttt_vs_friend|view_|add_|pay_rub|back_)"))
    app.add_handler(CallbackQueryHandler(ttt_menu, pattern="^ttt_menu$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # Запуск с вебхуком
    PORT = int(os.environ.get("PORT", 10000))
    if WEBHOOK_URL:
        # Устанавливаем вебхук и запускаем сервер
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    else:
        app.run_polling()
