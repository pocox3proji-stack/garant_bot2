

import asyncio
import logging
import os
import sqlite3
import secrets

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7262162132

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db = sqlite3.connect("deals.db")
db.execute("""
CREATE TABLE IF NOT EXISTS deals (
    code TEXT PRIMARY KEY,
    buyer INTEGER,
    seller INTEGER,
    status TEXT
)
""")
db.commit()


def menu(user_id):
    buttons = [
        [InlineKeyboardButton(text="🎁 Создать сделку", callback_data="create")],
        [InlineKeyboardButton(text="🔑 Присоединиться", callback_data="join")],
        [InlineKeyboardButton(text="📋 Мои сделки", callback_data="mine")],
        [InlineKeyboardButton(text="🆘 Помощь", callback_data="help")]
    ]

    if user_id == ADMIN_ID:
        buttons.append([
            InlineKeyboardButton(
                text="👑 Панель администратора",
                callback_data="admin"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🎁 THE GIFT GUARANTOR\n\n"
        "Добро пожаловать!\n"
        "Сервис сопровождения сделок между участниками.",
        reply_markup=menu(message.from_user.id)
    )


@dp.callback_query(F.data == "create")
async def create_deal(call: CallbackQuery):
    code = secrets.token_hex(4).upper()

    db.execute(
        "INSERT INTO deals VALUES (?, ?, ?, ?)",
        (code, call.from_user.id, None, "waiting")
    )
    db.commit()

    await call.message.answer(
        f"🎁 Сделка создана!\n\n"
        f"Код для второго участника: {code}\n\n"
        "Передайте этот код продавцу."
    )
    await call.answer()


@dp.callback_query(F.data == "join")
async def join_deal(call: CallbackQuery):
    await call.message.answer(
        "Отправьте код сделки отдельным сообщением."
    )
    await call.answer()


@dp.message(F.text)
async def text_handler(message: Message):
    code = message.text.strip().upper()

    deal = db.execute(
        "SELECT * FROM deals WHERE code = ?", (code,)
    ).fetchone()

    if deal:
        if deal[1] == message.from_user.id:
            await message.answer("Вы создали эту сделку.")
            return

        if deal[2] is not None:
            await message.answer("Эта сделка уже занята.")
            return

        if deal[3] != "waiting":
            await message.answer("Эта сделка недоступна.")
            return

        db.execute(
            "UPDATE deals SET seller = ?, status = ? WHERE code = ?",
            (message.from_user.id, "active", code)
        )
        db.commit()

        await message.answer("Вы присоединились к сделке!")
        await bot.send_message(
            deal[1],
            f"Второй участник присоединился к сделке {code}."
        )
        return

    await message.answer(
        "Используйте кнопки меню для управления сделками.",
        reply_markup=menu(message.from_user.id)
    )


@dp.callback_query(F.data == "mine")
async def my_deals(call: CallbackQuery):
    rows = db.execute(
        "SELECT code, status FROM deals WHERE buyer = ? OR seller = ?",
        (call.from_user.id, call.from_user.id)
    ).fetchall()

    if not rows:
        await call.message.answer("У вас пока нет сделок.")
    else:
        text = "📋 Ваши сделки:\n\n"
        for row in rows:
            text += f"Код: {row[0]} | Статус: {row[1]}\n"
        await call.message.answer(text)

    await call.answer()


@dp.callback_query(F.data == "help")
async def help_menu(call: CallbackQuery):
    await call.message.answer(
        "🆘 Помощь\n\n"
        "Создайте сделку и передайте её код второму участнику."
    )
    await call.answer()


@dp.callback_query(F.data == "admin")
async def admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    rows = db.execute(
        "SELECT code, buyer, seller, status FROM deals"
    ).fetchall()

    await call.message.answer(
        "👑 АДМИНИСТРАТОР\n\n"
        f"Всего сделок: {len(rows)}"
    )

    for row in rows:
        code, buyer, seller, status = row

        buttons = []

        if status != "paused":
            buttons.append(
                InlineKeyboardButton(
                    text="⛔ Заморозить",
                    callback_data=f"pause:{code}"
                )
            )
        else:
            buttons.append(
                InlineKeyboardButton(
                    text="▶️ Возобновить",
                    callback_data=f"resume:{code}"
                )
            )

        await call.message.answer(
            f"Сделка {code}\n"
            f"Покупатель: {buyer}\n"
            f"Продавец: {seller}\n"
            f"Статус: {status}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons])
        )

    await call.answer()


@dp.callback_query(F.data.startswith("pause:"))
async def pause_deal(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    code = call.data.split(":")[1]

    db.execute(
        "UPDATE deals SET status = 'paused' WHERE code = ?",
        (code,)
    )
    db.commit()

    row = db.execute(
        "SELECT buyer, seller FROM deals WHERE code = ?", (code,)
    ).fetchone()

    if row:
        for user_id in row:
            if user_id:
                await bot.send_message(
                    user_id,
                    f"⛔ Сделка {code} приостановлена администратором."
                )

    await call.message.answer("Сделка заморожена.")
    await call.answer()


@dp.callback_query(F.data.startswith("resume:"))
async def resume_deal(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Нет доступа", show_alert=True)
        return

    code = call.data.split(":")[1]

    db.execute(
        "UPDATE deals SET status = 'active' WHERE code = ?",
        (code,)
    )
    db.commit()

    await call.message.answer("Сделка возобновлена.")
    await call.answer()


async def main():
    logging.info("THE GIFT GUARANTOR запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
