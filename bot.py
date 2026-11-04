import asyncio
import logging
import os
import re
import secrets
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7262162132
DB_PATH = os.getenv("DB_PATH", "deals.db")

CARD_PAYMENT_DETAILS = os.getenv("CARD_PAYMENT_DETAILS", "").strip()
YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET", "").strip()
TON_WALLET_ADDRESS = os.getenv(
    "TON_WALLET_ADDRESS",
    "UQDxj9An8wquQrRspHI_SdhKK9rKLXon4-4xR7X64nQoJMCR",
).strip()
ADMIN_GIFT_USERNAME = os.getenv("ADMIN_GIFT_USERNAME", "").strip().lstrip("@")

if not BOT_TOKEN:
    raise RuntimeError("Переменная BOT_TOKEN не найдена")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("gift_guarantor")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# A single SQLite connection is used on the bot's one asyncio event loop.
db = sqlite3.connect(DB_PATH, timeout=30)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")
db.execute("PRAGMA busy_timeout=30000")


# ---------------------------------------------------------------------------
# Database and migrations. Existing deals.db data is preserved.
# ---------------------------------------------------------------------------

db.execute(
    """
    CREATE TABLE IF NOT EXISTS deals (
        code TEXT PRIMARY KEY,
        buyer INTEGER,
        seller INTEGER,
        status TEXT
    )
    """
)


def ensure_column(name: str, definition: str) -> None:
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(deals)").fetchall()
    }
    if name not in columns:
        db.execute(f"ALTER TABLE deals ADD COLUMN {name} {definition}")


ensure_column("amount_rub", "INTEGER")
ensure_column("resume_status", "TEXT")
ensure_column("payment_method", "TEXT")

db.execute(
    """
    CREATE TABLE IF NOT EXISTS pending_price (
        user_id INTEGER PRIMARY KEY,
        deal_code TEXT NOT NULL UNIQUE
    )
    """
)
db.commit()


def get_deal(code: str) -> Optional[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM deals WHERE code = ?", (code,)
    ).fetchone()


def is_participant(deal: sqlite3.Row, user_id: int) -> bool:
    return bool(deal and user_id in (deal["buyer"], deal["seller"]))


def format_rub(amount) -> str:
    return f"{int(amount):,}".replace(",", " ") + " ₽"


def safe_code(raw: str) -> Optional[str]:
    code = raw.strip().upper()
    return code if re.fullmatch(r"[A-F0-9]{8}", code) else None


def main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎁 Создать сделку", callback_data="create")],
        [InlineKeyboardButton(text="🔑 Присоединиться", callback_data="join")],
        [InlineKeyboardButton(text="📋 Мои сделки", callback_data="mine")],
        [InlineKeyboardButton(text="🆘 Помощь", callback_data="help")],
    ]
    if user_id == ADMIN_ID:
        rows.append(
            [InlineKeyboardButton(
                text="👑 Панель администратора", callback_data="admin"
            )]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirmation_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтвердить сумму", callback_data=f"confirm:{code}"
        )],
        [InlineKeyboardButton(
            text="❌ Отклонить сумму", callback_data=f"reject:{code}"
        )],
    ])


def payment_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💳 Банковская карта", callback_data=f"pay:CARD:{code}"
        )],
        [InlineKeyboardButton(
            text="🟡 ЮMoney", callback_data=f"pay:YOOMONEY:{code}"
        )],
        [InlineKeyboardButton(
            text="💵 USDT (сеть TON)", callback_data=f"pay:USDT:{code}"
        )],
        [InlineKeyboardButton(
            text="💎 TON", callback_data=f"pay:TON:{code}"
        )],
    ])


def buyer_paid_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Я оплатил", callback_data=f"buyer_paid:{code}"
        )]
    ])


def buyer_received_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎁 Я получил подарок", callback_data=f"buyer_gift_ok:{code}"
        )]
    ])


def admin_deal_keyboard(deal: sqlite3.Row) -> InlineKeyboardMarkup:
    code = deal["code"]
    status = deal["status"]
    rows = []

    if status == "awaiting_payment":
        rows.append([InlineKeyboardButton(
            text="✅ Подтвердить поступление оплаты",
            callback_data=f"admin_paid:{code}",
        )])
    elif status == "awaiting_gift":
        rows.append([InlineKeyboardButton(
            text="🎁 Подарок получен гарантом",
            callback_data=f"admin_gift_in:{code}",
        )])
    elif status == "gift_received":
        rows.append([InlineKeyboardButton(
            text="📤 Я передал подарок покупателю",
            callback_data=f"admin_gift_sent:{code}",
        )])
    elif status == "awaiting_buyer_gift":
        rows.append([InlineKeyboardButton(
            text="🔔 Повторить запрос подтверждения покупателю",
            callback_data=f"admin_remind_buyer:{code}",
        )])
    elif status == "awaiting_seller_payout":
        rows.append([InlineKeyboardButton(
            text="💸 Выплата продавцу отправлена — завершить",
            callback_data=f"admin_complete:{code}",
        )])

    if status == "paused":
        rows.append([InlineKeyboardButton(
            text="▶️ Возобновить", callback_data=f"resume:{code}"
        )])
    elif status != "completed":
        rows.append([InlineKeyboardButton(
            text="⛔ Заморозить", callback_data=f"pause:{code}"
        )])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_safely(
    user_id: Optional[int], text: str, reply_markup=None
) -> None:
    if not user_id:
        return
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
    except Exception:
        logger.exception("Не удалось отправить сообщение пользователю %s", user_id)


async def admin_notice(text: str) -> None:
    await notify_safely(ADMIN_ID, text)


def payment_instructions(method: str, code: str, amount: int) -> str:
    header = (
        f"Сделка: {code}\n"
        f"Сумма: {format_rub(amount)}\n\n"
    )

    if method == "YOOMONEY":
        if not YOOMONEY_WALLET:
            return header + "Оплата через ЮMoney пока не настроена."
        return (
            header
            + "🟡 Перевод через ЮMoney\n\n"
            + f"Номер кошелька:\n{YOOMONEY_WALLET}\n\n"
            + "Переведите точную сумму на указанный кошелёк. "
            + "После перевода нажмите кнопку «Я оплатил»."
        )

    if method == "CARD":
        if not CARD_PAYMENT_DETAILS:
            return (
                header
                + "Оплата картой пока не настроена: реквизиты ещё не добавлены "
                  "администратором. Не переводите деньги, пока бот не покажет "
                  "платёжные реквизиты."
            )
        return (
            header
            + "💳 Реквизиты для оплаты картой:\n"
            + CARD_PAYMENT_DETAILS
            + "\n\nПосле перевода дождитесь подтверждения поступления оплаты."
        )

    if method in ("USDT", "TON"):
        if not TON_WALLET_ADDRESS:
            return (
                header
                + "Криптовалютные реквизиты пока не настроены. "
                  "Не отправляйте средства."
            )
        asset = "USDT (Jetton, сеть TON)" if method == "USDT" else "TON"
        return (
            header
            + f"Способ оплаты: {asset}\n"
            + f"Кошелёк получателя:\n{TON_WALLET_ADDRESS}\n\n"
            + "Перед отправкой убедитесь, что выбран правильный актив и сеть. "
              "Сохраните подтверждение перевода. Оплата будет подтверждена "
              "администратором после проверки поступления."
        )

    return header + "Неизвестный способ оплаты."


# ---------------------------------------------------------------------------
# Start and deal creation
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎁 THE GIFT GUARANTOR\n\n"
        "Сервис сопровождения сделок между покупателем и продавцом.\n"
        "Статусы и дальнейшие действия по сделке отображаются здесь.",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.callback_query(F.data == "create")
async def create_deal(call: CallbackQuery):
    code = secrets.token_hex(4).upper()
    while get_deal(code):
        code = secrets.token_hex(4).upper()

    db.execute(
        "INSERT INTO deals (code, buyer, seller, status) "
        "VALUES (?, ?, NULL, 'waiting')",
        (code, call.from_user.id),
    )
    db.commit()

    await call.message.answer(
        f"🎁 Сделка создана!\n\nКод: {code}\n\n"
        "Передайте этот код продавцу. Не публикуйте его в открытом доступе."
    )
    await call.answer("Сделка создана")


@dp.callback_query(F.data == "join")
async def join_prompt(call: CallbackQuery):
    await call.message.answer(
        "Отправьте 8-значный код сделки отдельным сообщением."
    )
    await call.answer()


# ---------------------------------------------------------------------------
# Seller price input and joining
# ---------------------------------------------------------------------------

async def process_price(message: Message, code: str, raw_amount: str) -> None:
    user_id = message.from_user.id
    deal = get_deal(code)

    if (
        not deal
        or deal["status"] != "setting_price"
        or deal["seller"] != user_id
    ):
        db.execute(
            "DELETE FROM pending_price WHERE user_id = ?", (user_id,)
        )
        db.commit()
        await message.answer("Эта сделка больше не ожидает от вас цену.")
        return

    normalized = raw_amount.replace(" ", "").replace(",", ".")
    try:
        value = Decimal(normalized)
        if (
            not value.is_finite()
            or value <= 0
            or value > Decimal("1000000000")
        ):
            raise InvalidOperation
        if value != value.to_integral_value():
            await message.answer(
                "Укажите целое количество рублей, например 15000."
            )
            return
        amount = int(value)
    except (InvalidOperation, ValueError):
        await message.answer(
            "Не понял сумму. Введите целое число рублей, например 15000."
        )
        return

    cur = db.execute(
        "UPDATE deals SET amount_rub = ?, status = 'awaiting_confirmation' "
        "WHERE code = ? AND seller = ? AND status = 'setting_price'",
        (amount, code, user_id),
    )
    db.execute(
        "DELETE FROM pending_price WHERE user_id = ?", (user_id,)
    )
    db.commit()

    if cur.rowcount != 1:
        await message.answer(
            "Не удалось сохранить цену: статус сделки уже изменился."
        )
        return

    await message.answer(f"✅ Цена указана: {format_rub(amount)}")
    await notify_safely(
        deal["buyer"],
        f"💰 Продавец указал цену по сделке {code}: {format_rub(amount)}.\n\n"
        "Подтвердите сумму или отклоните её.",
        confirmation_keyboard(code),
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: Message):
    if not message.from_user:
        return

    text = (message.text or "").strip()
    user_id = message.from_user.id

    pending = db.execute(
        "SELECT deal_code FROM pending_price WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if pending:
        await process_price(message, pending["deal_code"], text)
        return

    code = safe_code(text)
    if not code:
        await message.answer(
            "Используйте кнопки меню. Для присоединения отправьте "
            "8-значный код сделки.",
            reply_markup=main_menu(user_id),
        )
        return

    deal = get_deal(code)
    if not deal:
        await message.answer("Сделка с таким кодом не найдена.")
        return
    if deal["buyer"] == user_id:
        await message.answer(
            "Вы создали эту сделку и не можете присоединиться как продавец."
        )
        return
    if deal["status"] != "waiting" or deal["seller"] is not None:
        await message.answer("Эта сделка уже занята или недоступна.")
        return

    pending_other = db.execute(
        "SELECT deal_code FROM pending_price WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if pending_other:
        await message.answer(
            f"Сначала укажите цену по сделке {pending_other['deal_code']}."
        )
        return

    cur = db.execute(
        "UPDATE deals SET seller = ?, status = 'setting_price' "
        "WHERE code = ? AND seller IS NULL AND status = 'waiting'",
        (user_id, code),
    )
    if cur.rowcount != 1:
        db.rollback()
        await message.answer("Не удалось присоединиться: сделка уже занята.")
        return

    db.execute(
        "INSERT OR REPLACE INTO pending_price (user_id, deal_code) "
        "VALUES (?, ?)",
        (user_id, code),
    )
    db.commit()

    await message.answer(
        f"🤝 Вы присоединились к сделке {code}.\n\n"
        "Теперь отправьте стоимость товара в целых рублях. Например: 15000"
    )
    await notify_safely(
        deal["buyer"],
        f"🤝 Продавец присоединился к сделке {code}. Ожидается цена."
    )


# ---------------------------------------------------------------------------
# Buyer confirms or rejects the price
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("confirm:"))
async def confirm_price(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)

    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка приостановлена.", show_alert=True)
        return

    cur = db.execute(
        "UPDATE deals SET status = 'awaiting_payment' "
        "WHERE code = ? AND buyer = ? AND status = 'awaiting_confirmation'",
        (code, call.from_user.id),
    )
    db.commit()

    if cur.rowcount != 1:
        await call.answer(
            "Сумма уже обработана или действие недоступно.",
            show_alert=True,
        )
        return

    await call.message.answer(
        f"✅ Сумма подтверждена: {format_rub(deal['amount_rub'])}\n\n"
        "Выберите способ оплаты:",
        reply_markup=payment_keyboard(code),
    )
    await notify_safely(
        deal["seller"],
        f"Покупатель подтвердил сумму {format_rub(deal['amount_rub'])} "
        f"по сделке {code}. Ожидается оплата."
    )
    await call.answer("Сумма подтверждена")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_price(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)

    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка приостановлена.", show_alert=True)
        return
    if not deal["seller"]:
        await call.answer("В сделке пока нет продавца.", show_alert=True)
        return

    cur = db.execute(
        "UPDATE deals SET status = 'setting_price', amount_rub = NULL, "
        "payment_method = NULL "
        "WHERE code = ? AND buyer = ? AND status = 'awaiting_confirmation'",
        (code, call.from_user.id),
    )
    if cur.rowcount == 1:
        db.execute(
            "INSERT OR REPLACE INTO pending_price (user_id, deal_code) "
            "VALUES (?, ?)",
            (deal["seller"], code),
        )
    db.commit()

    if cur.rowcount != 1:
        await call.answer(
            "Действие уже обработано или недоступно.",
            show_alert=True,
        )
        return

    await call.message.answer(
        "Сумма отклонена. Ожидается новая цена продавца."
    )
    await notify_safely(
        deal["seller"],
        f"❌ Покупатель отклонил цену по сделке {code}. "
        "Отправьте новую сумму в рублях."
    )
    await call.answer("Цена отклонена")


# ---------------------------------------------------------------------------
# Payment method selection. Payment confirmation is manual by the admin.
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("pay:"))
async def select_payment(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 3 or parts[1] not in ("CARD", "YOOMONEY", "USDT", "TON"):
        await call.answer("Некорректный способ оплаты.", show_alert=True)
        return

    _, method, code = parts
    deal = get_deal(code)

    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка приостановлена.", show_alert=True)
        return

    cur = db.execute(
        "UPDATE deals SET payment_method = ? "
        "WHERE code = ? AND buyer = ? AND status = 'awaiting_payment'",
        (method, code, call.from_user.id),
    )
    db.commit()

    if cur.rowcount != 1:
        await call.answer("Выбор оплаты сейчас недоступен.", show_alert=True)
        return

    instructions = payment_instructions(method, code, deal["amount_rub"])
    await call.message.answer(
        instructions,
        reply_markup=(
            buyer_paid_keyboard(code)
            if method == "YOOMONEY" and YOOMONEY_WALLET
            else None
        ),
    )

    if method == "CARD" and not CARD_PAYMENT_DETAILS:
        await call.answer("Реквизиты карты ещё не настроены.", show_alert=True)
    elif method == "YOOMONEY" and not YOOMONEY_WALLET:
        await call.answer("Кошелёк ЮMoney ещё не настроен.", show_alert=True)
    elif method in ("USDT", "TON") and not TON_WALLET_ADDRESS:
        await call.answer("Криптокошелёк ещё не настроен.", show_alert=True)
    else:
        await call.answer("Способ оплаты выбран")

    await admin_notice(
        f"💳 Покупатель выбрал оплату {method}.\n"
        f"Сделка: {code}\nСумма: {format_rub(deal['amount_rub'])}\n"
        "После фактической проверки поступления подтверди оплату в админ-панели."
    )
    await notify_safely(
        deal["seller"],
        f"Покупатель выбрал способ оплаты {method} по сделке {code}. "
        "Ожидается подтверждение поступления оплаты."
    )


@dp.callback_query(F.data.startswith("buyer_paid:"))
async def buyer_paid(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)

    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа к этой сделке.", show_alert=True)
        return

    if deal["status"] != "awaiting_payment":
        await call.answer("Сейчас нельзя отметить оплату.", show_alert=True)
        return

    if deal["payment_method"] != "YOOMONEY":
        await call.answer(
            "Для этой сделки выбран другой способ оплаты.",
            show_alert=True,
        )
        return

    await admin_notice(
        f"🔔 Покупатель сообщает об оплате через ЮMoney.\n"
        f"Сделка: {code}\n"
        f"Сумма: {format_rub(deal['amount_rub'])}\n"
        f"Покупатель: {deal['buyer']}\n\n"
        "Проверь поступление денег в ЮMoney. "
        "Если деньги действительно поступили, подтверди оплату в админ-панели."
    )

    await call.message.answer(
        "✅ Информация об оплате отправлена гаранту. "
        "Ожидайте проверки поступления денег."
    )
    await call.answer("Уведомление отправлено")


# ---------------------------------------------------------------------------
# Participant's deal list and help
# --------------------------------------------------------------------------------------

STATUS_LABELS = {
    "waiting": "Ожидается продавец",
    "setting_price": "Продавец указывает цену",
    "awaiting_confirmation": "Ожидается подтверждение цены",
    "awaiting_payment": "Ожидается оплата",
    "awaiting_gift": "Ожидается передача подарка гаранту",
    "gift_received": "Подарок получен гарантом",
    "awaiting_buyer_gift": "Ожидается подтверждение покупателя",
    "awaiting_seller_payout": "Ожидается выплата продавцу",
    "completed": "Сделка завершена",
    "paused": "Сделка приостановлена",
}


@dp.callback_query(F.data == "mine")
async def my_deals(call: CallbackQuery):
    rows = db.execute(
        "SELECT code, buyer, seller, status, amount_rub, payment_method "
        "FROM deals WHERE buyer = ? OR seller = ? ORDER BY rowid DESC LIMIT 30",
        (call.from_user.id, call.from_user.id),
    ).fetchall()

    if not rows:
        await call.message.answer("У вас пока нет сделок.")
    else:
        lines = ["📋 Ваши сделки:"]
        for row in rows:
            amount = (
                format_rub(row["amount_rub"])
                if row["amount_rub"] is not None
                else "не установлена"
            )
            method = row["payment_method"] or "не выбран"
            status = STATUS_LABELS.get(row["status"], row["status"])
            lines.append(
                f"\nКод: {row['code']}\nСтатус: {status}\n"
                f"Сумма: {amount}\nОплата: {method}"
            )
        await call.message.answer("\n".join(lines))
    await call.answer()


@dp.callback_query(F.data == "help")
async def help_menu(call: CallbackQuery):
    await call.message.answer(
        "🆘 Как работает сделка:\n\n"
        "1. Покупатель создаёт сделку и передаёт код продавцу.\n"
        "2. Продавец присоединяется и указывает цену.\n"
        "3. Покупатель подтверждает цену и выбирает способ оплаты.\n"
        "4. Поступление денег проверяется администратором вручную.\n"
        "5. После подтверждения оплаты продавец передаёт Telegram-подарок "
        "на личный аккаунт гаранта.\n"
        "6. Гарант проверяет подарок и передаёт его покупателю.\n"
        "7. Покупатель подтверждает получение, после чего администратор "
        "отмечает выплату продавцу и завершает сделку.\n\n"
        "Не отправляйте оплату, пока бот не показал актуальные реквизиты."
    )
    await call.answer()


# ---------------------------------------------------------------------------
# Admin panel and manual escrow workflow
# ---------------------------------------------------------------------------

def admin_only(call: CallbackQuery) -> bool:
    return call.from_user.id == ADMIN_ID


@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("Нет доступа.", show_alert=True)
        return

    rows = db.execute(
        "SELECT * FROM deals ORDER BY rowid DESC LIMIT 40"
    ).fetchall()

    await call.message.answer(
        f"👑 АДМИНИСТРАТОР\nСделок показано: {len(rows)}\n\n"
        "Подтверждай только те действия, которые проверил фактически."
    )

    for deal in rows:
        amount = (
            format_rub(deal["amount_rub"])
            if deal["amount_rub"] is not None
            else "не установлена"
        )
        status = STATUS_LABELS.get(deal["status"], deal["status"])
        text = (
            f"Сделка {deal['code']}\n"
            f"Покупатель: {deal['buyer']}\n"
            f"Продавец: {deal['seller']}\n"
            f"Сумма: {amount}\n"
            f"Оплата: {deal['payment_method'] or 'не выбрана'}\n"
            f"Статус: {status}"
        )
        await call.message.answer(
            text,
            reply_markup=admin_deal_keyboard(deal),
        )

    await call.answer()


async def admin_transition(
    call: CallbackQuery,
    code: str,
    expected_status: str,
    new_status: str,
    success_text: str,
    participant_text: str,
) -> Optional[sqlite3.Row]:
    if not admin_only(call):
        await call.answer("Нет доступа.", show_alert=True)
        return None

    deal = get_deal(code)
    if not deal:
        await call.answer("Сделка не найдена.", show_alert=True)
        return None
    if deal["status"] == "paused":
        await call.answer(
            "Сделка приостановлена. Сначала возобнови её.",
            show_alert=True,
        )
        return None

    cur = db.execute(
        "UPDATE deals SET status = ? "
        "WHERE code = ? AND status = ?",
        (new_status, code, expected_status),
    )
    db.commit()

    if cur.rowcount != 1:
        await call.answer(
            "Статус уже изменился или действие недоступно.",
            show_alert=True,
        )
        return None

    await call.message.answer(success_text)
    for uid in (deal["buyer"], deal["seller"]):
        if uid:
            await notify_safely(uid, participant_text)
    await call.answer("Статус обновлён")
    return deal


@dp.callback_query(F.data.startswith("admin_paid:"))
async def admin_confirm_payment(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = await admin_transition(
        call,
        code,
        "awaiting_payment",
        "awaiting_gift",
        f"✅ Оплата по сделке {code} отмечена как поступившая.",
        f"✅ Поступление оплаты по сделке {code} подтверждено. "
        "Следующий этап — передача Telegram-подарка гаранту.",
    )
    if not deal:
        return

    gift_target = (
        f"@{ADMIN_GIFT_USERNAME}"
        if ADMIN_GIFT_USERNAME
        else "аккаунт гаранта будет указан администратором"
    )
    await notify_safely(
        deal["seller"],
        f"🎁 Передача подарка по сделке {code}\n\n"
        f"Отправьте Telegram-подарок на личный аккаунт гаранта: {gift_target}.\n"
        "Важно: отправка подарка боту невозможна. После передачи дождитесь "
        "подтверждения получения в сделке."
    )
    await admin_notice(
        f"По сделке {code} подтверждена оплата. Ожидается Telegram-подарок "
        f"от продавца. Аккаунт для получения: {gift_target}."
    )


@dp.callback_query(F.data.startswith("admin_gift_in:"))
async def admin_confirm_gift_received(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = await admin_transition(
        call,
        code,
        "awaiting_gift",
        "gift_received",
        f"🎁 Получение подарка по сделке {code} подтверждено.",
        f"🎁 Гарант подтвердил получение подарка по сделке {code}. "
        "Ожидается его передача покупателю.",
    )
    if deal:
        await admin_notice(
            f"Подарок по сделке {code} отмечен как полученный. "
            "После фактической передачи покупателю нажми соответствующую кнопку."
        )


@dp.callback_query(F.data.startswith("admin_gift_sent:"))
async def admin_mark_gift_sent(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = await admin_transition(
        call,
        code,
        "gift_received",
        "awaiting_buyer_gift",
        f"📤 Передача подарка покупателю по сделке {code} отмечена.",
        f"🎁 По сделке {code} подарок передан покупателю. "
        "Подтверди получение кнопкой ниже.",
    )
    if deal:
        await notify_safely(
            deal["buyer"],
            f"🎁 Подарок по сделке {code} передан вам гарантом. "
            "Пожалуйста, проверьте получение.",
            buyer_received_keyboard(code),
        )


@dp.callback_query(F.data.startswith("admin_remind_buyer:"))
async def admin_remind_buyer(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("Нет доступа.", show_alert=True)
        return
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)
    if not deal or deal["status"] != "awaiting_buyer_gift":
        await call.answer(
            "Сделка не ожидает подтверждения покупателя.",
            show_alert=True,
        )
        return
    await notify_safely(
        deal["buyer"],
        f"Напоминание по сделке {code}: подтвердите получение Telegram-подарка.",
        buyer_received_keyboard(code),
    )
    await call.answer("Напоминание отправлено")


@dp.callback_query(F.data.startswith("buyer_gift_ok:"))
async def buyer_confirm_gift(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)
    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка приостановлена.", show_alert=True)
        return

    cur = db.execute(
        "UPDATE deals SET status = 'awaiting_seller_payout' "
        "WHERE code = ? AND buyer = ? AND status = 'awaiting_buyer_gift'",
        (code, call.from_user.id),
    )
    db.commit()
    if cur.rowcount != 1:
        await call.answer(
            "Подтверждение уже обработано или недоступно.",
            show_alert=True,
        )
        return

    await call.message.answer(
        "✅ Получение подарка подтверждено. Ожидается завершение выплаты продавцу."
    )
    await notify_safely(
        deal["seller"],
        f"Покупатель подтвердил получение подарка по сделке {code}. "
        "Ожидается завершение выплаты."
    )
    await admin_notice(
        f"Покупатель подтвердил получение подарка по сделке {code}. "
        "После фактической выплаты продавцу нажми "
        "«Выплата продавцу отправлена — завершить»."
    )
    await call.answer("Получение подтверждено")


@dp.callback_query(F.data.startswith("admin_complete:"))
async def admin_complete_deal(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = await admin_transition(
        call,
        code,
        "awaiting_seller_payout",
        "completed",
        f"🏁 Сделка {code} завершена.",
        f"🏁 Сделка {code} завершена. Спасибо за использование THE GIFT GUARANTOR!",
    )
    if deal:
        await admin_notice(
            f"Сделка {code} завершена. Отметка означает, что выплата продавцу "
            "фактически отправлена."
        )


# ---------------------------------------------------------------------------
# Pause/resume: admin can stop any deal. Resume restores prior status.
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("pause:"))
async def pause_deal(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("Нет доступа.", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    deal = get_deal(code)
    if not deal:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка уже приостановлена.")
        return
    if deal["status"] == "completed":
        await call.answer(
            "Завершённую сделку нельзя приостановить.",
            show_alert=True,
        )
        return

    cur = db.execute(
        "UPDATE deals SET resume_status = status, status = 'paused' "
        "WHERE code = ? AND status = ?",
        (code, deal["status"]),
    )
    db.commit()
    if cur.rowcount != 1:
        await call.answer("Не удалось изменить статус.", show_alert=True)
        return

    db.execute(
        "DELETE FROM pending_price WHERE deal_code = ?", (code,)
    )
    db.commit()

    for uid in (deal["buyer"], deal["seller"]):
        if uid:
            await notify_safely(
                uid,
                f"⛔ Сделка {code} временно приостановлена. "
                "Действия по ней пока недоступны."
            )
    await call.message.answer(f"⛔ Сделка {code} приостановлена.")
    await call.answer("Сделка приостановлена")


@dp.callback_query(F.data.startswith("resume:"))
async def resume_deal(call: CallbackQuery):
    if not admin_only(call):
        await call.answer("Нет доступа.", show_alert=True)
        return

    code = call.data.split(":", 1)[1]
    deal = get_deal(code)
    if not deal or deal["status"] != "paused":
        await call.answer(
            "Сделка не найдена или не приостановлена.",
            show_alert=True,
        )
        return

    previous = deal["resume_status"] or "waiting"
    cur = db.execute(
        "UPDATE deals SET status = ?, resume_status = NULL "
        "WHERE code = ? AND status = 'paused'",
        (previous, code),
    )
    db.commit()
    if cur.rowcount != 1:
        await call.answer("Не удалось возобновить сделку.", show_alert=True)
        return

    if previous == "setting_price" and deal["seller"]:
        db.execute(
            "INSERT OR REPLACE INTO pending_price (user_id, deal_code) "
            "VALUES (?, ?)",
            (deal["seller"], code),
        )
        db.commit()

    status_text = STATUS_LABELS.get(previous, previous)
    for uid in (deal["buyer"], deal["seller"]):
        if uid:
            await notify_safely(
                uid,
                f"▶️ Сделка {code} возобновлена. Текущий этап: {status_text}."
            )
    await call.message.answer(f"▶️ Сделка {code} возобновлена.")
    await call.answer("Сделка возобновлена")


# ---------------------------------------------------------------------------
# Startup / shutdown
# --------------------------------------------------------------------------------------

async def main():
    logger.info("THE GIFT GUARANTOR запущен")
    try:
        await dp.start_polling(bot)
    finally:
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
