import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import Column, Integer, String, Text, select, delete

# Настройка
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
# Преобразуем формат адреса базы для SQLAlchemy
DATABASE_URL = os.getenv("DATABASE_URL").replace("postgres://", "postgresql+asyncpg://")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

# Инициализация БД
Base = declarative_base()
class Task(Base):
    __tablename__ = 'tasks'
    id = Column(Integer, primary_key=True)
    name = Column(Text)
    done = Column(Integer, default=0)

engine = create_async_engine(DATABASE_URL)
async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- Твой старый функционал ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # (Тут оставим твою клавиатуру)
    await message.answer("Бот успешно запущен на PostgreSQL!")

# --- Логика работы с БД (Пример) ---
async def get_tasks():
    async with async_session() as session:
        result = await session.execute(select(Task))
        return result.scalars().all()

# --- Настройка Webhook ---
async def handle_webhook(request):
    text = await request.text()
    update = types.Update.model_validate_json(text)
    await dp.feed_update(bot, update)
    return web.Response(text="OK")

async def on_startup(app):
    # Создание таблиц при запуске
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"Бот запущен. База данных подключена. Вебхук: {WEBHOOK_URL}")

app = web.Application()
app.router.add_post(WEBHOOK_PATH, handle_webhook)
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, port=int(os.getenv("PORT", 10000)))