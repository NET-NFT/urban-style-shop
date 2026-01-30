import json
import os
import logging
import random
import re
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, InputMediaPhoto
from telegram.error import BadRequest
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
MAX_GAMES_PER_DAY = 10
MAX_PROMOS_PER_DAY = 2
MIN_GAMES_TO_LOSE = 5  # Бот проигрывает после 5 игр

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Хранение данных ===
from collections import defaultdict
import time
# ... существующие переменные ...
user_carts = {}
active_promocodes = set()  # Множество активных промокодов
user_game_stats = defaultdict(lambda: {"games": [], "promos": 0})
games = {}  # Для крестиков-ноликов
active_games = {}      # Игры между двумя игроками
pending_invites = {}   # Ожидающие приглашения
# === Защита от спама ===
user_last_action = defaultdict(float)

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
    code = "WIN" + str(random.randint(1000, 9999))
    active_promocodes.add(code)  # Сохраняем как активный
    return code

# === Защита от спама ===
async def rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = time.time()
    
    if now - user_last_action[user_id] < 1.0:  # 1 сек между действиями
        await update.callback_query.answer("⏳ Подождите немного!")
        return True
        
    user_last_action[user_id] = now
    return False

def find_losing_move(board, player):
    """Находит ход, который приведёт к победе игрока (бот проигрывает)"""
    for i in range(9):
        if board[i] == " ":
            board[i] = player
            if check_win(board, player):
                board[i] = " "  # Отменяем изменение
                return i
            board[i] = " "
    return None

def find_winning_move(board, player):
    """Находит ход, который приведёт к победе указанного игрока"""
    for i in range(9):
        if board[i] == " ":
            board[i] = player
            if check_win(board, player):
                board[i] = " "  # Отменяем изменение
                return i
            board[i] = " "
    return None

def check_game_limits(user_id: int):
    """Возвращает (can_play: bool, can_win: bool)"""
    now = time.time()
    
    # Очистка старых игр (>24ч)
    stats = user_game_stats[user_id]
    stats["games"] = [ts for ts in stats["games"] if now - ts < 86400]
    
    total_games = len(stats["games"])
    promo_count = stats["promos"]
    
    can_play = total_games < MAX_GAMES_PER_DAY  # 10 игр/день
    can_win = promo_count < MAX_PROMOS_PER_DAY  # 2 промокода/день
    
    return can_play, can_win

# === Обработчики магазина ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Сохраняем user_id при первом контакте
    context.user_data['session_user_id'] = update.effective_user.id
    
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
        [InlineKeyboardButton("↓↓ Игры ↓↓", callback_data="ignore")],
        [InlineKeyboardButton("🎮 Крестики-нолики", callback_data="ttt_menu")]
    ])

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):    
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = context.user_data.get('session_user_id', update.effective_user.id)

    # ЛОГИРОВАНИЕ
    logger.info(f"Получен callback: {data} от пользователя {user_id}")

    # Защита от спама
    if await rate_limit(update, context):
        return

    # Валидация данных
    if len(data) > 50 or not re.match(r"^[a-zA-Z0-9_\-]+$", data):
        await query.answer("Недопустимый запрос")
        logger.warning(f"Подозрительный callback_data: {data} от пользователя {user_id}")
        return
    
        # Управление количеством
    elif data.startswith("inc_"):
        prod_id = int(data.split("_")[1])
        user_id = update.effective_user.id
    
        MAX_TOTAL_ITEMS = 20
        current_cart = user_carts.get(user_id, {})
        total_items = sum(current_cart.values())
    
        if total_items >= MAX_TOTAL_ITEMS:
            # Показываем ошибку прямо в корзине
            await query.edit_message_text(
                "🛒 Корзина переполнена!\nМаксимум 20 товаров. Удалите что-нибудь или уменьшите количество.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Вернуться в корзину", callback_data="cart")]
                ])
            )
            return
    
        if user_id not in user_carts:
            user_carts[user_id] = {}
        user_carts[user_id][prod_id] = user_carts[user_id].get(prod_id, 0) + 1
        await show_cart(update, context)
        return

    elif data.startswith("dec_"):
        prod_id = int(data.split("_")[1])
        if user_id in user_carts and prod_id in user_carts[user_id]:
            user_carts[user_id][prod_id] -= 1
            if user_carts[user_id][prod_id] <= 0:
                del user_carts[user_id][prod_id]
        await show_cart(update, context)
        return

    elif data.startswith("del_"):
        prod_id = int(data.split("_")[1])
        if user_id in user_carts and prod_id in user_carts[user_id]:
            del user_carts[user_id][prod_id]
        await show_cart(update, context)
        return
        
    elif data == "cat_clothing":
        logger.info("Загружаем категорию clothing")
        await show_category(update, context, "clothing")
    elif data == "cat_shoes":
        await show_category(update, context, "shoes")
    elif data == "cat_accessories":
        await show_category(update, context, "accessories")
    elif data == "back_categories":
        if query.message.photo:
            await query.edit_message_caption(
                caption="Выберите категорию:",
                reply_markup=category_menu()
            )
        else:
            await query.edit_message_text(
                "Выберите категорию:",
                reply_markup=category_menu()
            )
    elif data.startswith("view_"):
        prod_id = int(data.split("_")[1])
        await view_product(update, context, prod_id)
    elif data.startswith("add_"):
        prod_id = int(data.split("_")[1])
        user_id = update.effective_user.id
    
        MAX_TOTAL_ITEMS = 20
        current_cart = user_carts.get(user_id, {})
        total_items = sum(current_cart.values())
    
        if total_items >= MAX_TOTAL_ITEMS:
            # Показываем ошибку в карточке товара
            product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
            if product:
                caption = f"*{product['name']}*\n\n{product['description']}\n\n⚠️ Нельзя добавить: корзина заполнена (макс. 20)."
                keyboard = [
                    [InlineKeyboardButton("⬅️ Назад", callback_data=f"back_cat_{product['category']}")]
                ]
                if product.get("photo_url", "").strip():
                    try:
                        await query.edit_message_media(
                            media=InputMediaPhoto(
                                media=product["photo_url"].strip(),
                                caption=caption,
                                parse_mode="Markdown"
                            ),
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                    except Exception:
                        await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
                else:
                    await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            else:
                await query.edit_message_text("❌ Товар не найден.")
            return
    
        if user_id not in user_carts:
            user_carts[user_id] = {}
        user_carts[user_id][prod_id] = user_carts[user_id].get(prod_id, 0) + 1
        await query.answer("✅ Товар добавлен!")
        await view_product(update, context, prod_id)
        return
    
    elif data == "cart":
        await show_cart(update, context)
    elif data == "pay_rub":
        await send_rub_invoice(update, context)
    elif data.startswith("back_cat_"):
        category = data.split("_")[2]
        # Удаляем текущее сообщение (фото или текст)
        await query.delete_message()
        # Отправляем новое текстовое меню категории
        items = [p for p in PRODUCTS if p["category"] == category]
        if not items:
            await update.effective_chat.send_message(
                "В этой категории нет товаров.",
                reply_markup=back_kb()
            )
        else:
            buttons = [[InlineKeyboardButton(p["name"], callback_data=f"view_{p['id']}")] for p in items]
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")])
            await update.effective_chat.send_message(
                "Выберите товар:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
    elif data == "back_categories":
        await query.delete_message()
        await update.effective_chat.send_message(
            "Выберите категорию:",
            reply_markup=category_menu()
        )
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
    elif data == "enter_promo":
        await query.edit_message_text(
            "Введите промокод:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Отмена", callback_data="cart")]
            ])
        )
        # Ожидаем текстовый ввод
        context.user_data['awaiting_promo'] = True

async def view_product(update: Update, context: ContextTypes.DEFAULT_TYPE, prod_id: int):
    query = update.callback_query
    try:
        product = next((p for p in PRODUCTS if p["id"] == prod_id), None)
        if not product:
            await query.edit_message_text("❌ Товар не найден. Возможно, он удалён.")
            return

        photo_url = product.get("photo_url", "").strip()
        caption = f"*{product['name']}*\n\n{product['description']}\n\nЦена: {product['price_rub']} ₽"
        keyboard = [
            [InlineKeyboardButton("➕ В корзину", callback_data=f"add_{prod_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"back_cat_{product['category']}")]
        ]

        if photo_url:
            try:
                if not photo_url.startswith(("http://", "https://")):
                    raise ValueError("Неверный URL фото")
                    
                await query.edit_message_media(
                    media=InputMediaPhoto(media=photo_url, caption=caption, parse_mode="Markdown"),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    # Игнорируем ошибку — пользователь уже видит это сообщение
                    pass
                else:
                    logger.error(f"Ошибка фото: {e}")
                    await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e:
                logger.error(f"Ошибка загрузки фото: {e}")
                await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            try:
                await query.edit_message_text(
                    caption, 
                    parse_mode="Markdown", 
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    pass  # Игнорируем
                else:
                    raise
    except Exception as e:
        logger.error(f"Критическая ошибка в view_product: {e}")
        await query.edit_message_text("Произошла ошибка. Попробуйте позже.")

async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str):    
    query = update.callback_query
    items = [p for p in PRODUCTS if p["category"] == category]
    if not items:
        await query.edit_message_text("В этой категории нет товаров.", reply_markup=back_kb())
        return

    buttons = [[InlineKeyboardButton(p["name"], callback_data=f"view_{p['id']}")] for p in items]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")])

    # ВСЕГДА используем edit_message_text для категорий
    await query.edit_message_text(
        "Выберите товар:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_promo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_promo'):
        promo = update.message.text.strip().upper()

        # === Проверка длины промокода ===
        if len(promo) > 20:
            await update.message.reply_text("❌ Слишком длинный промокод")
            return
        if not re.match(r"^[A-Z0-9]+$", promo):
            await update.message.reply_text("❌ Промокод может содержать только буквы и цифры")
            return   
        if promo in active_promocodes:
            context.user_data['promo'] = promo
            await update.message.reply_text("✅ Промокод применён! Скидка 200 ₽ активна.")
        else:
            await update.message.reply_text("❌ Неверный промокод.")
        
        context.user_data['awaiting_promo'] = False
        # Показываем обновлённую корзину
        await show_cart_from_message(update, context)
        return True
    return False

async def show_cart_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = context.user_data.get('session_user_id', update.effective_user.id)
    cart = user_carts.get(user_id, {})
    if not cart:
        await update.message.reply_text("Корзина пуста.", reply_markup=back_kb())
        return

    total = 0
    for pid, qty in cart.items():
        product = next((p for p in PRODUCTS if p["id"] == pid), None)
        if product:
            total += product["price_rub"] * qty

    promo = context.user_data.get('promo', None)
    discount = 200 if promo in active_promocodes else 0
    final_total = max(total - discount, 0)

    text = "🛒 *Ваша корзина:*\n\n"
    for pid, qty in cart.items():
        product = next((p for p in PRODUCTS if p["id"] == pid), None)
        if product:
            text += f"- {product['name']} × {qty}\n"
    
    if discount > 0:
        text += f"\nСкидка по промокоду: -{discount} ₽"
    
    text += f"\n*Итого: {final_total} ₽*"

    kb = []
    if not promo:
        kb.append([InlineKeyboardButton("🎟️ Ввести промокод", callback_data="enter_promo")])
    kb.extend([
        [InlineKeyboardButton("💳 Оплатить", callback_data="pay_rub")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")]
    ])

    await update.message.reply_text(
        text, 
        parse_mode="Markdown", 
        reply_markup=InlineKeyboardMarkup(kb)
    )
        
def back_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")]
    ])

def calculate_cart_total(user_id: int, context: ContextTypes.DEFAULT_TYPE = None) -> int:
    """
    Возвращает общую сумму корзины в рублях (без копеек)
    Учитывает промокод, если context передан
    """
    cart = user_carts.get(user_id, {})
    total = 0
    
    # Считаем базовую сумму
    for pid, qty in cart.items():
        product = next((p for p in PRODUCTS if p["id"] == pid), None)
        if product:
            total += product["price_rub"] * qty

    # Применяем скидку по промокоду
    if context and hasattr(context, 'user_data'):
        promo = context.user_data.get('promo')
        if promo in active_promocodes:
            total = max(total - 200, 0)  # Минимальная сумма — 0
    
    return total

async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = context.user_data.get('session_user_id', update.effective_user.id)
    cart = user_carts.get(user_id, {})
    promo = context.user_data.get('promo', None)
    
    if not cart:
        await query.edit_message_text("Корзина пуста.", reply_markup=back_kb())
        return

    total = 0
    buttons = []
    
    for pid, qty in cart.items():
        product = next((p for p in PRODUCTS if p["id"] == pid), None)
        if not product:
            continue
            
        total += product["price_rub"] * qty
        
        # Кнопки управления
        control_buttons = [
            InlineKeyboardButton("-", callback_data=f"dec_{pid}"),
            InlineKeyboardButton(str(qty), callback_data="ignore"),
            InlineKeyboardButton("+", callback_data=f"inc_{pid}")
        ]
        buttons.append([InlineKeyboardButton(f"{product['name']} × {qty}", callback_data=f"view_{pid}")])
        buttons.append(control_buttons)
        buttons.append([InlineKeyboardButton("🗑️ Удалить", callback_data=f"del_{pid}")])
        buttons.append([])  # Пустая строка для разделения

    # Применяем скидку
    discount = 200 if promo in active_promocodes else 0
    final_total = max(total - discount, 0)

    text = "🛒 *Ваша корзина:*\n\n"
    if discount > 0:
        text += f"\nСкидка по промокоду: -{discount} ₽"
    
    text += f"\n*Итого: {final_total} ₽*"

    kb = []
    if not promo:
        kb.append([InlineKeyboardButton("🎟️ Ввести промокод", callback_data="enter_promo")])
    kb.extend([
        [InlineKeyboardButton("💳 Оплатить", callback_data="pay_rub")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_categories")]
    ])
    buttons.extend(kb)

    await query.edit_message_text(
        text, 
        parse_mode="Markdown", 
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def send_rub_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = context.user_data.get('session_user_id', update.effective_user.id)
    cart = user_carts.get(user_id, {})
    
    if not cart:
        await query.edit_message_text("Корзина пуста.")
        return

    total_rub = 0
    for pid, qty in cart.items():
        product = next((p for p in PRODUCTS if p["id"] == pid), None)
        if product:
            total_rub += product["price_rub"] * qty

    # Применяем скидку
    promo = context.user_data.get('promo')
    if promo in active_promocodes:
        total_rub = max(total_rub - 200, 0)

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
    payment = update.message.successful_payment
    user_id = context.user_data.get('session_user_id', update.effective_user.id)
    user = update.effective_user
    username = user.username or f"id{user.id}"

       # === Проверка валюты ===
    if payment.currency != "RUB":
        logger.warning(f"Неверная валюта: {payment.currency} от пользователя {user_id}")
        await update.message.reply_text("❌ Ошибка оплаты: неверная валюта.")
        return

        # === Проверка суммы с учётом промокода ===
    expected_amount = calculate_cart_total(user_id, context) * 100  # в копейках
    if payment.total_amount != expected_amount:
        logger.warning(f"Несоответствие суммы: ожидаемо {expected_amount}, получено {payment.total_amount} от {user_id}")
        await update.message.reply_text("❌ Ошибка оплаты: сумма не совпадает. Свяжитесь с поддержкой.")
        return

    # === Обработка заказа ===
    if user_id in user_carts:
        del user_carts[user_id]
    
    # Деактивируем промокод после использования
    if context.user_data.get('promo') in active_promocodes:
        active_promocodes.remove(context.user_data['promo'])
        context.user_data.pop('promo', None)

    username = user.username or f"id{user.id}"
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"✅ *Новый заказ!* \nПользователь: @{user.username}\nСумма: {payment.total_amount // 100} ₽",
        parse_mode="Markdown"
    )
    await update.message.reply_text("🎉 Спасибо за заказ! Менеджер свяжется с вами.")

# === Обработчики игры ===
async def start_ttt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    can_play, _ = check_game_limits(user_id)
    
    if not can_play:
        await update.message.reply_text(
            f"🎮 Лимит игр на сегодня исчерпан ({MAX_GAMES_PER_DAY}/день)."
        )
        return
    
    logger.info("Запуск игры с ботом")
    chat_id = update.effective_chat.id
    board = create_game_board()
    games[chat_id] = {'board': board, 'vs_bot': True}
    
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
    chat_id = query.message.chat.id
    user_id = context.user_data.get('session_user_id', update.effective_user.id)
    MAX_GAMES_PER_DAY = 10

    # Игра с ботом
    if chat_id in games:
        game = games[chat_id]
        board = game['board']
        move_index = int(query.data.split('_')[1])

        if board[move_index] != " ":
            await query.answer("Эта ячейка уже занята!")
            return

        # Ход игрока (X)
        board[move_index] = 'X'

        # Проверка победы игрока
    if check_win(board, 'X'):
        _, can_win = check_game_limits(user_id)
    
        if can_win:
            # Выдаём промокод
            promo = generate_promo()
            result_text = f"🎉 Вы победили! 🎉\n\nТвой промокод: `{promo}`\n+30 ⭐️ бонусов!"
            user_game_stats[user_id]["promos"] += 1
        else:
            # Победа без промокода
            result_text = "🎉 Вы победили! Но лимит промокодов на сегодня исчерпан."
    
        user_game_stats[user_id]["games"].append(time.time())  # Записываем игру
        del games[chat_id]
        await query.edit_message_text(text=result_text, parse_mode="Markdown")
        return

    # Проверка ничьей
    if check_draw(board):
        user_game_stats[user_id]["games"].append(time.time())
        result_text = "🤝 Ничья!"
        del games[chat_id]
    
        # ЗАПИСЫВАЕМ ИГРУ В ИСТОРИЮ
        user_id = update.effective_user.id
        user_game_history[user_id].append(time.time())
    
        await query.edit_message_text(text=result_text, reply_markup=None)
        return

    # === ХОД БОТА (O) ===
    empty_cells = [i for i, cell in enumerate(board) if cell == " "]
    if not empty_cells:
        return

    user_id = update.effective_user.id
    _, can_win = check_game_limits(user_id)

    bot_move = None
    if can_win:
        # Бот играет честно: сначала атакует, потом защищается
        bot_move = find_winning_move(board, 'O')
        if bot_move is None:
            bot_move = find_winning_move(board, 'X')
    else:
        # Бот намеренно проигрывает: ищет ход, который даст победу игроку
        bot_move = find_losing_move(board, 'X')
    
    # Если не нашли ход — выбираем случайно
    if bot_move is None:
        bot_move = random.choice(empty_cells)

    board[bot_move] = 'O'

    # Проверка победы бота (только если он играет честно)
    if can_win and check_win(board, 'O'):
        result_text = "🤖 Бот победил! Попробуй ещё раз!"
        del games[chat_id]
        await query.edit_message_text(text=result_text, reply_markup=None)
        return

    # Проверка ничьей после хода бота
    if check_draw(board):
        result_text = "🤝 Ничья!"
        del games[chat_id]
        user_game_history[user_id].append(time.time())  # ← Записываем ничью
        await query.edit_message_text(text=result_text, reply_markup=None)
        return

    # Обновление доски
    await query.edit_message_text(
        text="Ваш ход:",
        reply_markup=get_game_keyboard(board)
    )
        
    # Мультиплеерная игра (если есть)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_promo_input))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(inc_|dec_|del_|cat_|cart|ttt_game|ttt_menu|ttt_vs_bot|ttt_vs_friend|view_|add_|pay_rub|back_|enter_promo)"))
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
