
import asyncio
import logging
import os
import sqlite3
import secrets
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7262162132

TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =========================
# DATABASE
# =========================

db = sqlite3.connect("deals.db")
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS deals (
    code TEXT PRIMARY KEY,
    buyer INTEGER,
    seller INTEGER,
    status TEXT
)
""")

def add_column(name, definition):
    columns = [
        row["name"]
        for row in db.execute("PRAGMA table_info(deals)").fetchall()
    ]
    if name not in columns:
        db.execute(
            f"ALTER TABLE deals ADD COLUMN {name} {definition}"
        )

add_column("amount_rub", "INTEGER")
add_column("resume_status", "TEXT")
add_column("payment_method", "TEXT")

db.commit()


def get_deal(code):
    return db.execute(
        "SELECT * FROM deals WHERE code = ?", (code,)
    ).fetchone()


def is_paused(code):
    deal = get_deal(code)
    return not deal or deal["status"] == "paused"


# =========================
# FSM
# =========================

class DealStates(StatesGroup):
    waiting_price = State()


# =========================
# KEYBOARDS
# =========================

def menu(user_id):
    buttons = [
        [InlineKeyboardButton(
            text="🎁 Создать сделку",
            callback_data="create"
        )],
        [InlineKeyboardButton(
            text="🔑 Присоединиться",
            callback_data="join"
        )],
        [InlineKeyboardButton(
            text="📋 Мои сделки",
            callback_data="mine"
        )],
        [InlineKeyboardButton(
            text="🆘 Помощь",
            callback_data="help"
        )]
    ]

    if user_id == ADMIN_ID:
        buttons.append([
            InlineKeyboardButton(
                text="👑 Панель администратора",
                callback_data="admin"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_keyboard(code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Подтвердить сумму",
                callback_data=f"confirm:{code}"
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"reject:{code}"
            )
        ]
    ])


def payment_keyboard(code):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💵 USDT (TON)",
                callback_data=f"pay:USDT:{code}"
            )
        ],
        [
            InlineKeyboardButton(
                text="💎 TON",
                callback_data=f"pay:TON:{code}"
            )
        ]
    ])


# =========================
# START
# =========================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    await message.answer(
        "🎁 THE GIFT GUARANTOR\n\n"
        "Добро пожаловать!\n"
        "Сервис сопровождения сделок между участниками.",
        reply_markup=menu(message.from_user.id)
    )


# =========================
# CREATE DEAL
# =========================

@dp.callback_query(F.data == "create")
async def create_deal(call: CallbackQuery):
    code = secrets.token_hex(4).upper()

    db.execute(
        """
        INSERT INTO deals (code, buyer, seller, status)
        VALUES (?, ?, ?, ?)
        """,
        (code, call.from_user.id, None, "waiting")
    )
    db.commit()

    await call.message.answer(
        "🎁 Сделка создана!\n\n"
        f"Код сделки: {code}\n\n"
        "Передайте код продавцу."
    )
    await call.answer()


# =========================
# JOIN DEAL
# =========================

@dp.callback_query(F.data == "join")
async def join_deal(call: CallbackQuery):
    await call.message.answer(
        "Отправьте код сделки отдельным сообщением."
    )
    await call.answer()


@dp.message(DealStates.waiting_price)
async def receive_price(message: Message, state: FSMContext):
    data = await state.get_data()
    code = data["deal_code"]

    deal = get_deal(code)

    if not deal or deal["status"] != "setting_price":
        await state.clear()
        await message.answer("Сделка больше недоступна.")
        return

    try:
        value = Decimal(message.text.replace(",", ".").strip())

        if value <= 0 or value > 1000000000:
            raise InvalidOperation

        if value != value.to_integral_value():
            await message.answer(
                "Укажите целое количество рублей."
            )
            return

        amount = int(value)

    except (InvalidOperation, ValueError):
        await message.answer(
            "Введите корректную сумму в рублях.\n"
            "Например: 15000"
        )
        return

    db.execute(
        """
        UPDATE deals
        SET amount_rub = ?, status = 'awaiting_confirmation'
        WHERE code = ? AND seller = ? AND status = 'setting_price'
        """,
        (amount, code, message.from_user.id)
    )
    db.commit()

    await state.clear()

    await message.answer(
        f"✅ Стоимость сделки установлена: {amount:,} ₽".replace(",", " ")
    )

    await bot.send_message(
        deal["buyer"],
        f"💰 Продавец установил стоимость сделки {code}:\n\n"
        f"**{amount:,} ₽**\n\n"
        "Подтвердите или отклоните сумму.".replace(",", " "),
        reply_markup=confirm_keyboard(code),
        parse_mode="Markdown"
    )


@dp.message(F.text)
async def text_handler(message: Message):
    code = message.text.strip().upper()

    if len(code) != 8:
        await message.answer(
            "Используйте кнопки меню для управления сделками.",
            reply_markup=menu(message.from_user.id)
        )
        return

    deal = get_deal(code)

    if not deal:
        await message.answer("Сделка с таким кодом не найдена.")
        return

    if deal["buyer"] == message.from_user.id:
        await message.answer("Вы создали эту сделку.")
        return

    if deal["seller"] is not None:
        await message.answer("Эта сделка уже занята.")
        return

    if deal["status"] != "waiting":
        await message.answer("Эта сделка недоступна.")
        return

    db.execute(
        """
        UPDATE deals
        SET seller = ?, status = 'setting_price'
        WHERE code = ? AND seller IS NULL AND status = 'waiting'
        """,
        (message.from_user.id, code)
    )
    db.commit()

    updated = get_deal(code)

    if updated["seller"] != message.from_user.id:
        await message.answer("Не удалось присоединиться к сделке.")
        return

    await message.answer(
        "🤝 Вы присоединились к сделке!\n\n"
        "Теперь укажите стоимость товара в рублях.\n"
        "Например: 15000"
    )

    await bot.send_message(
        deal["buyer"],
        f"🤝 Продавец присоединился к сделке {code}.\n"
        "Ожидается указание стоимости."
    )

    # FSM for seller
    # Set state through the dispatcher storage
    await bot.send_message(
        message.from_user.id,
        "Введите сумму отдельным сообщением."
    )


# =========================
# PRICE ENTRY COMMAND
# =========================

@dp.message(Command("price"))
async def price_command(message: Message, state: FSMContext):
    rows = db.execute(
        """
        SELECT code FROM deals
        WHERE seller = ? AND status = 'setting_price'
        """,
        (message.from_user.id,)
    ).fetchall()

    if not rows:
        await message.answer("У вас нет сделок, ожидающих цены.")
        return

    if len(rows) > 1:
        await message.answer(
            "У вас несколько сделок. Отправьте код сделки, "
            "чтобы выбрать нужную."
        )
        return

    code = rows[0]["code"]
    await state.update_data(deal_code=code)
    await state.set_state(DealStates.waiting_price)

    await message.answer("Введите стоимость в целых рублях.")


# =========================
# BUYER CONFIRMATION
# =========================

@dp.callback_query(F.data.startswith("confirm:"))
async def confirm_price(call: CallbackQuery):
    code = call.data.split(":")[1]
    deal = get_deal(code)

    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа.", show_alert=True)
        return

    if is_paused(code):
        await call.answer("Сделка заморожена.", show_alert=True)
        return

    if deal["status"] != "awaiting_confirmation":
        await call.answer("Сумма уже обработана.", show_alert=True)
        return

    db.execute(
        """
        UPDATE deals SET status = 'awaiting_payment'
        WHERE code = ? AND status = 'awaiting_confirmation'
        """,
        (code,)
    )
    db.commit()

    await call.message.answer(
        "✅ Сумма подтверждена!\n\n"
        f"Стоимость: {deal['amount_rub']:,} ₽\n\n"
        "Выберите способ оплаты:".replace(",", " "),
        reply_markup=payment_keyboard(code)
    )

    await bot.send_message(
        deal["seller"],
        "Покупатель подтвердил стоимость сделки."
    )

    await call.answer()


@dp.callback_query(F.data.startswith("reject:"))
async def reject_price(call: CallbackQuery):
    code = call.data.split(":")[1]
    deal = get_deal(code)

    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа.", show_alert=True)
        return

    if deal["status"] != "awaiting_confirmation":
        await call.answer("Действие недоступно.", show_alert=True)
        return

    db.execute(
        """
        UPDATE deals
        SET status = 'setting_price', amount_rub = NULL
        WHERE code = ?
        """,
        (code,)
    )
    db.commit()

    await call.message.answer(
        "Вы отклонили стоимость. Продавец может указать новую сумму."
    )

    await bot.send_message(
        deal["seller"],
        "❌ Покупатель отклонил стоимость. Укажите новую цену командой /price."
    )

    await call.answer()


# =========================
# PAYMENT SELECTION
# =========================

@dp.callback_query(F.data.startswith("pay:"))
async def select_payment(call: CallbackQuery):
    _, method, code = call.data.split(":")
    deal = get_deal(code)

    if not deal or deal["buyer"] != call.from_user.id:
        await call.answer("Нет доступа.", show_alert=True)
        return

    if is_paused(code):
        await call.answer("Сделка заморожена.", show_alert=True)
        return

    if deal["status"] != "awaiting_payment":
        await call.answer("Оплата сейчас недоступна.", show_alert=True)
        return

    db.execute(
        """
        UPDATE deals SET payment_method = ?
        WHERE code = ? AND status = 'awaiting_payment'
        """,
        (method, code)
    )
    db.commit()

    await call.message.answer(
        f"Вы выбрали оплату: {method}.\n\n"
        "Платёжный модуль ещё не подключён.\n"
        "Пожалуйста, пока не отправляйте деньги.\n\n"
        "После подключения автоматической проверки "
        "здесь появятся реквизиты и кнопка проверки оплаты."
    )

    await call.answer()


# =========================
# MY DEALS
# =========================

@dp.callback_query(F.data == "mine")
async def my_deals(call: CallbackQuery):
    rows = db.execute(
        """
        SELECT code, status, amount_rub, payment_method
        FROM deals
        WHERE buyer = ? OR seller = ?
        """,
        (call.from_user.id, call.from_user.id)
    ).fetchall()

    if not rows:
        await call.message.answer("У вас пока нет сделок.")
    else:
        text = "📋 Ваши сделки:\n\n"

        for row in rows:
            text += (
                f"Код: {row['code']}\n"
                f"Статус: {row['status']}\n"
                f"Сумма: {row['amount_rub'] or 'не установлена'} ₽\n"
                f"Оплата: {row['payment_method'] or 'не выбрана'}\n\n"
            )

        await call.message.answer(text)

    await call.answer()


# =========================
# HELP
# =========================

@dp.callback_query(F.data == "help")
async def help_menu(call: CallbackQuery):
    await call.message.answer(
        "🆘 Помощь\n\n"
        "1. Покупатель создаёт сделку.\n"
        "2. Продавец присоединяется по коду.\n"
        "3. Продавец указывает цену.\n"
        "4. Покупатель подтверждает цену.\n"
        "5. Покупатель выбирает способ оплаты.\n"
    )
    await call.answer()


# =========================
# ADMIN PANEL
# =========================

@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return

    rows = db.execute(
        """
        SELECT code, buyer, seller, status, amount_rub
        FROM deals
        ORDER BY rowid DESC
        """
    ).fetchall()

    await call.message.answer(
        f"👑 АДМИНИСТРАТОР\n\nВсего сделок: {len(rows)}"
    )

    for row in rows:
        code = row["code"]
        status = row["status"]

        if status != "paused":
            buttons = [[
                InlineKeyboardButton(
                    text="⛔ Заморозить",
                    callback_data=f"pause:{code}"
                )
            ]]
        else:
            buttons = [[
                InlineKeyboardButton(
                    text="▶️ Возобновить",
                    callback_data=f"resume:{code}"
                )
            ]]

        await call.message.answer(
            f"Сделка {code}\n"
            f"Покупатель: {row['buyer']}\n"
            f"Продавец: {row['seller']}\n"
            f"Сумма: {row['amount_rub'] or 'не установлена'} ₽\n"
            f"Статус: {status}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=buttons
            )
        )

    await call.answer()


# =========================
# PAUSE DEAL
# =========================

@dp.callback_query(F.data.startswith("pause:"))
async def pause_deal(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return

    code = call.data.split(":")[1]
    deal = get_deal(code)

    if not deal:
        await call.answer("Сделка не найдена.", show_alert=True)
        return

    if deal["status"] == "paused":
        await call.answer("Уже заморожена.")
        return

    db.execute(
        """
        UPDATE deals
        SET resume_status = status, status = 'paused'
        WHERE code = ?
        """,
        (code,)
    )
    db.commit()

    for user_id in (deal["buyer"], deal["seller"]):
        if user_id:
            await bot.send_message(
                user_id,
                f"⛔ Сделка {code} заморожена администратором."
            )

    await call.message.answer("⛔ Сделка заморожена.")
    await call.answer()


# =========================
# RESUME DEAL
# =========================

@dp.callback_query(F.data.startswith("resume:"))
async def resume_deal(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа.", show_alert=True)
        return

    code = call.data.split(":")[1]
    deal = get_deal(code)

    if not deal or deal["status"] != "paused":
        await call.answer("Сделка не найдена или не заморожена.")
        return

    new_status = deal["resume_status"] or "active"

    db.execute(
        """
        UPDATE deals
        SET status = ?, resume_status = NULL
        WHERE code = ?
        """,
        (new_status, code)
    )
    db.commit()

    await call.message.answer("▶️ Сделка возобновлена.")

    for user_id in (deal["buyer"], deal["seller"]):
        if user_id:
            await bot.send_message(
                user_id,
                f"▶️ Сделка {code} возобновлена администратором."
            )

    await call.answer()


# =========================
# RUN
# =========================

async def main():
    logging.info("THE GIFT GUARANTOR запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
