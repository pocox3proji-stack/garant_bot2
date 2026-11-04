
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7262162132

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! 👋\n\n"
        "Бот успешно запущен!\n"
        "Я готов к работе."
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "Доступные команды:\n"
        "/start — запуск бота\n"
        "/help — помощь"
    )


@dp.message()
async def other_messages(message: Message):
    await message.answer(
        "Я получил твоё сообщение! ✅\n"
        "Бот работает."
    )


async def main():
    logging.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
