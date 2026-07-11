import asyncio
import os
import json
import logging
import gspread_asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from google.oauth2.service_account import Credentials
from aiohttp import web

# Логирование
logging.basicConfig(level=logging.INFO)

# Инициализация бота
TOKEN = os.getenv("TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- GOOGLE SHEETS КОНФИГУРАЦИЯ ---
def get_creds():
    creds_dict = json.loads(os.getenv("GOOGLE_CREDS"))
    # Добавляем нужные области доступа (scopes)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    return Credentials.from_service_account_info(creds_dict, scopes=scopes)

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

async def get_sheet():
    agcm_client = await agcm.authorize()
    spreadsheet = await agcm_client.open("BotData")
    return await spreadsheet.get_worksheet(0)

# --- FSM И КЛАВИАТУРЫ ---
class PlannerStates(StatesGroup):
    waiting_for_tasks = State()

user_tasks = {}
main_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Планер"), KeyboardButton(text="Список задач")]], resize_keyboard=True)
planner_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="День"), KeyboardButton(text="Неделя"), KeyboardButton(text="Месяц")], [KeyboardButton(text="Назад")]], resize_keyboard=True)
finish_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Это всё")]], resize_keyboard=True)

# --- ОБРАБОТЧИКИ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Главное меню", reply_markup=main_kb)

@dp.message(F.text == "Планер")
async def show_planner(message: types.Message):
    await message.answer("Выберите период", reply_markup=planner_kb)

@dp.message(F.text == "Назад")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню", reply_markup=main_kb)

@dp.message(F.text == "День")
async def start_day_planner(message: types.Message, state: FSMContext):
    await state.set_state(PlannerStates.waiting_for_tasks)
    user_tasks[message.from_user.id] = []
    await message.answer("Пиши цели. Когда закончишь, нажми 'Это всё'.", reply_markup=finish_kb)

@dp.message(PlannerStates.waiting_for_tasks)
async def process_day_tasks(message: types.Message, state: FSMContext):
    if message.text == "Это всё":
        tasks = user_tasks.get(message.from_user.id, [])
        sheet = await get_sheet()
        # Очищаем старые данные (заголовки оставляем)
        await sheet.clear()
        await sheet.append_row(["task", "done"])
        for task in tasks:
            await sheet.append_row([task, "0"])
        await message.answer("Цели сохранены в таблицу!", reply_markup=main_kb)
        await state.clear()
    else:
        user_tasks[message.from_user.id].append(message.text)
        await message.answer("Принято, что еще?", reply_markup=finish_kb)

@dp.message(F.text == "Список задач")
async def list_tasks(message: types.Message):
    sheet = await get_sheet()
    rows = await sheet.get_all_values()
    if len(rows) <= 1:
        await message.answer("Список пуст.")
        return
    text = "Твои цели:\n"
    for i, row in enumerate(rows[1:], 1):
        status = "✅" if row[1] == "1" else "❌"
        text += f"{i}. {row[0]} {status}\n"
    await message.answer(text + "\nДля отметки напиши: done номер")

@dp.message(Command("done"))
async def mark_done(message: types.Message):
    try:
        task_num = int(message.text.split()[1])
        sheet = await get_sheet()
        await sheet.update_cell(task_num + 1, 2, "1")
        await message.answer("Отметил!")
        await list_tasks(message)
    except:
        await message.answer("Ошибка. Напиши: done номер")

# --- ВЕБХУК ---
async def handle_ping(request):
    return web.Response(text="I am alive!")

async def on_startup(app):
    # Эта строчка сама регистрирует твоего бота в Telegram при каждом запуске
    webhook_url = f"{os.getenv('RENDER_EXTERNAL_URL')}/{TOKEN}"
    await bot.set_webhook(webhook_url)
    logging.info(f"Вебхук автоматически установлен на {webhook_url}")

def main():
    app = web.Application()
    app.router.add_post(f"/{TOKEN}", handle_webhook)
    app.router.add_get("/", handle_ping)  # <--- Допиши эту строку
    web.run_app(app, host='0.0.0.0', port=10000)

if __name__ == "__main__":
    main()
