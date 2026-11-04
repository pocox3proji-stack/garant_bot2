import asyncio
import logging
import os
import re
import secrets
import sqlite3
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7262162132
DB_PATH = os.getenv("DB_PATH", "deals.db")

if not BOT_TOKEN:
    raise RuntimeError("Переменная BOT_TOKEN не найдена")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Используем прежний deals.db и не удаляем существующие сделки.
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")

db.execute("""
CREATE TABLE IF NOT EXISTS deals (
    code TEXT PRIMARY KEY,
    buyer INTEGER,
    seller INTEGER,
    status TEXT
)
""")


def ensure_column(name: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute("PRAGMA table_info(deals)")}
    if name not in columns:
        db.execute(f"ALTER TABLE deals ADD COLUMN {name} {definition}")


ensure_column("amount_rub", "INTEGER")
ensure_column("resume_status", "TEXT")
ensure_column("payment_method", "TEXT")

# Храним ожидаемый ввод цены в БД, чтобы он не пропадал при рестарте Railway.
db.execute("""
CREATE TABLE IF NOT EXISTS pending_price (
    user_id INTEGER PRIMARY KEY,
    deal_code TEXT NOT NULL UNIQUE
)
""")
db.commit()


def get_deal(code: str):
    return db.execute("SELECT * FROM deals WHERE code = ?", (code,)).fetchone()


def participant(deal, user_id: int) -> bool:
    return bool(deal and user_id in (deal["buyer"], deal["seller"]))


def fmt_rub(amount) -> str:
    return f"{int(amount):,}".replace(",", " ") + " ₽"


def main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🎁 Создать сделку", callback_data="create")],
        [InlineKeyboardButton(text="🔑 Присоединиться", callback_data="join")],
        [InlineKeyboardButton(text="📋 Мои сделки", callback_data="mine")],
        [InlineKeyboardButton(text="🆘 Помощь", callback_data="help")],
    ]
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton(text="👑 Панель администратора", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirmation_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить сумму", callback_data=f"confirm:{code}")],
        [InlineKeyboardButton(text="❌ Отклонить сумму", callback_data=f"reject:{code}")],
    ])


def payment_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 USDT (сеть TON)", callback_data=f"pay:USDT:{code}")],
        [InlineKeyboardButton(text="💎 TON", callback_data=f"pay:TON:{code}")],
    ])


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎁 THE GIFT GUARANTOR\n\n"
        "Сервис сопровождения сделок между покупателем и продавцом.",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.callback_query(F.data == "create")
async def create_deal(call: CallbackQuery):
    code = secrets.token_hex(4).upper()
    db.execute(
        "INSERT INTO deals (code, buyer, seller, status) VALUES (?, ?, NULL, 'waiting')",
        (code,),
    )
    db.commit()
    await call.message.answer(
        f"🎁 Сделка создана!\n\nКод: {code}\n\nПередайте код продавцу."
    )
    await call.answer()


@dp.callback_query(F.data == "join")
async def join_prompt(call: CallbackQuery):
    await call.message.answer("Отправьте код сделки отдельным сообщением.")
    await call.answer()


async def notify_safely(user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except Exception:
        logger.exception("Не удалось отправить сообщение пользователю %s", user_id)


async def process_price(message: Message, code: str, raw_amount: str) -> None:
    deal = get_deal(code)
    if not deal or deal["status"] != "setting_price" or deal["seller"] != message.from_user.id:
        db.execute("DELETE FROM pending_price WHERE user_id = ?", (message.from_user.id,))
        db.commit()
        await message.answer("Эта сделка больше не ожидает от вас цену.")
        return

    try:
        value = Decimal(raw_amount.replace(" ", "").replace(",", "."))
        if not value.is_finite() or value <= 0 or value > 1_000_000_000:
            raise InvalidOperation
        if value != value.to_integral_value():
            await message.answer("Укажите целое количество рублей, например 15000.")
            return
        amount = int(value)
    except (InvalidOperation, ValueError):
        await message.answer("Не понял сумму. Введите целое число рублей, например 15000.")
        return

    cur = db.execute(
        "UPDATE deals SET amount_rub = ?, status = 'awaiting_confirmation' "
        "WHERE code = ? AND seller = ? AND status = 'setting_price'",
        (amount, code, message.from_user.id),
    )
    db.execute("DELETE FROM pending_price WHERE user_id = ?", (message.from_user.id,))
    db.commit()
    if cur.rowcount != 1:
        await message.answer("Не удалось сохранить цену: статус сделки уже изменился.")
        return

    await message.answer(f"✅ Цена установлена: {fmt_rub(amount)}")
    await notify_safely(
        deal["buyer"],
        f"💰 Продавец указал цену по сделке {code}: {fmt_rub(amount)}\n\n"
        "Подтвердите сумму или отклоните её.",
    )
    # Клавиатура отправляется отдельным сообщением покупателю.
    await notify_safely(deal["buyer"], "Выберите действие:")
    try:
        await bot.send_message(deal["buyer"], "Подтверждение цены:", reply_markup=confirmation_keyboard(code))
    except Exception:
        logger.exception("Не удалось отправить кнопки подтверждения")


@dp.message(F.text)
async def text_handler(message: Message):
    text = (message.text or "").strip()
    user_id = message.from_user.id

    pending = db.execute(
        "SELECT deal_code FROM pending_price WHERE user_id = ?", (user_id,)
    ).fetchone()
    if pending:
        await process_price(message, pending["deal_code"], text)
        return

    code = text.upper()
    if not re.fullmatch(r"[A-F0-9]{8}", code):
        await message.answer("Используйте кнопки меню. Для присоединения отправьте 8-значный код сделки.", reply_markup=main_menu(user_id))
        return

    deal = get_deal(code)
    if not deal:
        await message.answer("Сделка с таким кодом не найдена.")
        return
    if deal["buyer"] == user_id:
        await message.answer("Вы создали эту сделку и не можете присоединиться как продавец.")
        return
    if deal["status"] != "waiting" or deal["seller"] is not None:
        await message.answer("Эта сделка уже занята или недоступна.")
        return

    # Не допускаем несколько одновременно ожидающих ввода цены сделок у одного продавца.
    pending_other = db.execute("SELECT deal_code FROM pending_price WHERE user_id = ?", (user_id,)).fetchone()
    if pending_other:
        await message.answer(f"Сначала укажите цену по сделке {pending_other['deal_code']}.")
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
    db.execute("INSERT OR REPLACE INTO pending_price (user_id, deal_code) VALUES (?, ?)", (user_id, code))
    db.commit()

    await message.answer(
        f"🤝 Вы присоединились к сделке {code}.\n\n"
        "Теперь сразу отправьте стоимость товара в целых рублях. Например: 15000"
    )
    await notify_safely(deal["buyer"], f"🤝 Продавец присоединился к сделке {code}. Ожидается цена.")


@dp.callback_query(F.data.startswith("confirm:"))
async def confirm_price(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)
    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка заморожена администратором.", show_alert=True)
        return
    cur = db.execute(
        "UPDATE deals SET status = 'awaiting_payment' "
        "WHERE code = ? AND buyer = ? AND status = 'awaiting_confirmation'",
        (code, call.from_user.id),
    )
    db.commit()
    if cur.rowcount != 1:
        await call.answer("Сумма уже обработана или действие недоступно.", show_alert=True)
        return
    await call.message.answer(
        f"✅ Сумма подтверждена: {fmt_rub(deal['amount_rub'])}\n\nВыберите способ оплаты:",
        reply_markup=payment_keyboard(code),
    )
    await notify_safely(deal["seller"], f"Покупатель подтвердил сумму {fmt_rub(deal['amount_rub'])} по сделке {code}.")
    await call.answer()


@dp.callback_query(F.data.startswith("reject:"))
async def reject_price(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)
    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка заморожена администратором.", show_alert=True)
        return
    cur = db.execute(
        "UPDATE deals SET status = 'setting_price', amount_rub = NULL, payment_method = NULL "
        "WHERE code = ? AND buyer = ? AND status = 'awaiting_confirmation'",
        (code, call.from_user.id),
    )
    if cur.rowcount == 1:
        db.execute("INSERT OR REPLACE INTO pending_price (user_id, deal_code) VALUES (?, ?)", (deal["seller"], code))
    db.commit()
    if cur.rowcount != 1:
        await call.answer("Действие уже обработано или недоступно.", show_alert=True)
        return
    await call.message.answer("Сумма отклонена. Ожидается новая цена продавца.")
    await notify_safely(deal["seller"], f"❌ Покупатель отклонил цену по сделке {code}. Отправьте новую сумму в рублях.")
    await call.answer()


@dp.callback_query(F.data.startswith("pay:"))
async def select_payment(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 3 or parts[1] not in ("USDT", "TON"):
        await call.answer("Некорректный способ оплаты.", show_alert=True)
        return
    _, method, code = parts
    deal = get_deal(code)
    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа к этой сделке.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка заморожена администратором.", show_alert=True)
        return
    cur = db.execute(
        "UPDATE deals SET payment_method = ? WHERE code = ? AND buyer = ? AND status = 'awaiting_payment'",
        (method, code, call.from_user.id),
    )
    db.commit()
    if cur.rowcount != 1:
        await call.answer("Выбор оплаты сейчас недоступен.", show_alert=True)
        return
    await call.message.answer(
        f"Выбран способ оплаты: {method}.\n\n"
        "⚠️ Приём и автоматическая проверка платежей пока не подключены. "
        "Не отправляйте деньги по этой сделке."
    )
    await notify_safely(deal["seller"], f"Покупатель выбрал способ оплаты {method} по сделке {code}. Платёж ещё не активирован.")
    await call.answer()


@dp.callback_query(F.data == "mine")
async def my_deals(call: CallbackQuery):
    rows = db.execute(
        "SELECT code, buyer, seller, status, amount_rub, payment_method "
        "FROM deals WHERE buyer = ? OR seller = ? ORDER BY rowid DESC",
        (call.from_user.id, call.from_user.id),
    ).fetchall()
    if not rows:
        await call.message.answer("У вас пока нет сделок.")
    else:
        lines = ["📋 Ваши сделки:"]
        for row in rows:
            amount = fmt_rub(row["amount_rub"]) if row["amount_rub"] is not None else "не установлена"
            method = row["payment_method"] or "не выбран"
            lines.append(f"\nКод: {row['code']}\nСтатус: {row['status']}\nСумма: {amount}\nОплата: {method}")
        await call.message.answer("\n".join(lines))
    await call.answer()


@dp.callback_query(F.data == "help")
async def help_menu(call: CallbackQuery):
    await call.message.answer(
        "🆘 Как работает сделка:\n\n"
        "1. Покупатель создаёт сделку и отправляет код продавцу.\n"
        "2. Продавец присоединяется и сразу указывает цену в рублях.\n"
        "3. Покупатель подтверждает или отклоняет цену.\n"
        "4. Покупатель выбирает USDT или TON.\n\n"
        "Приём криптовалюты пока не активирован: не отправляйте деньги через бота."
    )
    await call.answer()


@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    rows = db.execute(
        "SELECT code, buyer, seller, status, amount_rub FROM deals ORDER BY rowid DESC LIMIT 50"
    ).fetchall()
    await call.message.answer(f"👑 АДМИНИСТРАТОР\nСделок показано: {len(rows)}")
    for row in rows:
        amount = fmt_rub(row["amount_rub"]) if row["amount_rub"] is not None else "не установлена"
        if row["status"] == "paused":
            buttons = [[InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"resume:{row['code']}")]]
        else:
            buttons = [[InlineKeyboardButton(text="⛔ Заморозить", callback_data=f"pause:{row['code']}")]]
        await call.message.answer(
            f"Сделка {row['code']}\nПокупатель: {row['buyer']}\nПродавец: {row['seller']}\n"
            f"Сумма: {amount}\nСтатус: {row['status']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    await call.answer()


@dp.callback_query(F.data.startswith("pause:"))
async def pause_deal(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)
    if not deal:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    if deal["status"] == "paused":
        await call.answer("Сделка уже заморожена.")
        return
    db.execute("UPDATE deals SET resume_status = status, status = 'paused' WHERE code = ?", (code,))
    db.commit()
    for uid in (deal["buyer"], deal["seller"]):
        if uid:
            await notify_safely(uid, f"⛔ Сделка {code} заморожена администратором. Все действия по ней остановлены.")
    await call.message.answer(f"⛔ Сделка {code} заморожена.")
    await call.answer()


@dp.callback_query(F.data.startswith("resume:"))
async def resume_deal(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return
    code = call.data.split(":", 1)[1]
    deal = get_deal(code)
    if not deal or deal["status"] != "paused":
        await call.answer("Сделка не найдена или не заморожена.", show_alert=True)
        return
    previous = deal["resume_status"] or "waiting"
    db.execute("UPDATE deals SET status = ?, resume_status = NULL WHERE code = ? AND status = 'paused'", (previous, code))
    db.commit()
    if previous == "setting_price" and deal["seller"]:
        db.execute("INSERT OR REPLACE INTO pending_price (user_id, deal_code) VALUES (?, ?)", (deal["seller"], code))
        db.commit()
    for uid in (deal["buyer"], deal["seller"]):
        if uid:
            await notify_safely(uid, f"▶️ Сделка {code} возобновлена администратором. Статус: {previous}.")
    await call.message.answer(f"▶️ Сделка {code} возобновлена.")
    await call.answer()


async def main():
    logger.info("THE GIFT GUARANTOR запущен")
    try:
        await dp.start_polling(bot)
    finally:
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
