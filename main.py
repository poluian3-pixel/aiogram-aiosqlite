import asyncio
import os
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# Настройка логирования
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Состояния FSM
class PlannerStates(StatesGroup):
    waiting_for_tasks = State()

user_tasks = {}

# Клавиатуры
main_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Планер"), KeyboardButton(text="Список задач")]], resize_keyboard=True)
planner_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="День"), KeyboardButton(text="Неделя"), KeyboardButton(text="Месяц")], [KeyboardButton(text="Назад")]], resize_keyboard=True)
finish_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Это всё")]], resize_keyboard=True)

async def init_db():
    async with aiosqlite.connect("tasks.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, name TEXT, done INTEGER DEFAULT 0)")
        await db.commit()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Главное меню:", reply_markup=main_kb)

@dp.message(F.text == "Планер")
async def show_planner(message: types.Message):
    await message.answer("Выберите период:", reply_markup=planner_kb)

@dp.message(F.text == "Назад")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_kb)

# --- ЛОГИКА ДНЯ ---
@dp.message(F.text == "День")
async def start_day_planner(message: types.Message, state: FSMContext):
    await state.set_state(PlannerStates.waiting_for_tasks)
    user_tasks[message.from_user.id] = []
    await message.answer("Пиши цели по одной. Когда закончишь, нажми 'Это всё'.", reply_markup=finish_kb)

@dp.message(PlannerStates.waiting_for_tasks)
async def process_day_tasks(message: types.Message, state: FSMContext):
    if message.text == "Это всё":
        tasks = user_tasks.get(message.from_user.id, [])
        async with aiosqlite.connect("tasks.db") as db:
            await db.execute("DELETE FROM tasks")
            for task in tasks:
                await db.execute("INSERT INTO tasks (name, done) VALUES (?, 0)", (task,))
            await db.commit()
            
        text = "Твои цели на день:\n\n"
        for i, task in enumerate(tasks, 1):
            text += f"{i}. {task}\n"
        await message.answer(text, reply_markup=main_kb)
        await state.clear()
    else:
        user_tasks[message.from_user.id].append(message.text)
        await message.answer("Принято, что еще?")

@dp.message(F.text == "Список задач")
async def list_tasks(message: types.Message):
    async with aiosqlite.connect("tasks.db") as db:
        async with db.execute("SELECT id, name, done FROM tasks") as cursor:
            rows = await cursor.fetchall()
            
    if not rows:
        await message.answer("Список пуст.")
        return
        
    text = "Твои цели на день:\n\n"
    for i, row in enumerate(rows, 1):
        status = "✅" if row[2] else "❌"
        text += f"{i}. {row[1]} {status}\n"
    await message.answer(text + "\nДля отметки напиши: /done номер")

@dp.message(Command("done"))
async def mark_done(message: types.Message):
    try:
        task_num = int(message.text.split()[1])
        async with aiosqlite.connect("tasks.db") as db:
            async with db.execute("SELECT id FROM tasks LIMIT ? OFFSET ?", (1, task_num-1)) as cursor:
                row = await cursor.fetchone()
                if row:
                    await db.execute("UPDATE tasks SET done = 1 WHERE id = ?", (row[0],))
                    await db.commit()
                    await message.answer("Отметил!")
                    await list_tasks(message)
    except:
        await message.answer("Ошибка. Напиши: /done номер")

# --- Исправленный веб-сервер ---
async def handle(request):
    return web.Response(text="Bot is running smoothly 24/7!")

async def main():
    await init_db()
    
    # Конфигурируем веб-приложение
    app = web.Application()
    app.router.add_get('/', handle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 10000)
    await site.start()
    logging.info("Веб-сервер запущен на порту 10000")

    # Запускаем бота параллельно серверу
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())