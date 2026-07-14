import asyncio
import json
import logging
import datetime
import aiosqlite
import random
import gspread_asyncio
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Логирование
logging.basicConfig(level=logging.INFO)

# --- GOOGLE SHEETS ИНТЕГРАЦИЯ ---
def get_creds():
    creds_dict = json.loads(os.getenv("GOOGLE_CREDS"))
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    return Credentials.from_service_account_info(creds_dict, scopes=scopes)

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

async def get_sheet():
    agcm_client = await agcm.authorize()
    spreadsheet = await agcm_client.open("BotData")
    return await spreadsheet.get_worksheet(0)

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "test_bot_database.db"

router = Router()

# Состояния FSM
class TaskStates(StatesGroup):
    waiting_for_task_text = State()

class FocusStates(StatesGroup):
    waiting_for_focus_text = State()

# ==================== БАЗА ДАННЫХ ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                gender TEXT,
                current_streak INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                task_text TEXT,
                period TEXT,
                is_completed INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_focuses (
                user_id INTEGER,
                focus_text TEXT,
                PRIMARY KEY (user_id, focus_text)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS weekly_stats (
                user_id INTEGER,
                date TEXT,
                focus_text TEXT,
                PRIMARY KEY (user_id, date, focus_text)
            )
        """)
        await db.commit()

async def add_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def update_user_gender(user_id: int, gender: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET gender = ? WHERE user_id = ?", (gender, user_id))
        await db.commit()

async def get_user_gender(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def add_task(user_id: int, text: str, period: str):
    # 1. Сохраняем в локальную БД для скорости
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tasks (user_id, task_text, period) VALUES (?, ?, ?)", (user_id, text, period))
        await db.commit()
    
    # 2. Синхронизация с Google Таблицей как бекап
    try:
        sheet = await get_sheet()
        await sheet.append_row([user_id, text, period, 0]) # 0 - статус "не выполнено"
        logging.info(f"Задача для {user_id} успешно записана в Google Таблицу")
    except Exception as e:
        logging.error(f"Не удалось записать в Google Таблицу: {e}")

async def get_tasks(user_id: int, period: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT task_id, task_text, is_completed FROM tasks WHERE user_id = ? AND period = ?", (user_id, period)) as cursor:
            return await cursor.fetchall()

async def complete_task_db(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET is_completed = 1 WHERE task_id = ?", (task_id,))
        await db.commit()

async def delete_task_db(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        await db.commit()


# ==================== КЛАВИАТУРЫ ====================
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Планер"), KeyboardButton(text="📋 Список задач")],
            [KeyboardButton(text="⚙️ Настройка Фокусов"), KeyboardButton(text="🛡 Духовная поддержка")]
        ],
        resize_keyboard=True
    )

def get_period_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 День"), KeyboardButton(text="📝 Неделя"), KeyboardButton(text="📝 Месяц")],
            [KeyboardButton(text="🔙 Назад в меню")]
        ],
        resize_keyboard=True
    )

def get_task_management_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить цель"), KeyboardButton(text="✅ Выполнить цель")],
            [KeyboardButton(text="🗑 Удалить цель"), KeyboardButton(text="🔙 Назад к периодам")]
        ],
        resize_keyboard=True
    )

def get_stop_adding_menu():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📥 Это всё (завершить)")]],
        resize_keyboard=True
    )

def get_spiritual_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Лень"), KeyboardButton(text="Упадок сил")],
            [KeyboardButton(text="Тревога"), KeyboardButton(text="Потеря веры")],
            [KeyboardButton(text="🔙 Назад в меню")]
        ],
        resize_keyboard=True
    )


# ==================== ОБРАБОТЧИКИ (ХЭНДЛЕРЫ) ====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    await add_user(user_id)
    gender = await get_user_gender(user_id)
    
    if gender:
        welcome = "Привет, бро! Рад тебя видеть. Ты готов к новым целям? Выбирай раздел 👇" if gender == "male" else "Привет! Рад тебя видеть. Выбирай нужный раздел в меню ниже 👇"
        await message.answer(welcome, reply_markup=get_main_menu())
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="🙋‍♂️ Мужской", callback_data="gender_male")
        builder.button(text="🙋‍♀️ Женский", callback_data="gender_female")
        builder.adjust(2)
        await message.answer("Привет! Добро пожаловать в смарт-планер.\nУкажи свой пол для настройки окончаний:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("gender_"))
async def process_gender(callback: CallbackQuery):
    user_id = callback.from_user.id
    gender_choice = callback.data.split("_")[1]
    await update_user_gender(user_id, gender_choice)
    text = "Профиль настроен! Ты активировал мужской профиль. Погнали! 🎯" if gender_choice == "male" else "Профиль настроен! Ты активировала женский профиль. Продуктивного дня! ✨"
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text, reply_markup=get_main_menu())
    await callback.answer()

@router.message(F.text == "📅 Планер")
@router.message(F.text == "🔙 Назад к периодам")
async def cmd_planer(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Выбери период планирования:", reply_markup=get_period_menu())

@router.message(F.text == "📋 Список задач")
async def show_daily_list(message: Message, state: FSMContext):
    tasks = await get_tasks(message.from_user.id, "day")
    total = len(tasks)
    done = sum(1 for t in tasks if t[2] == 1)
    percent = int((done / total) * 100) if total > 0 else 0
    text = f"📋 **Твой список задач на сегодня:**\n📊 Прогресс: **{percent}%** ({done} из {total})\n\n"
    if not tasks:
        text += "Список на день пуст."
    else:
        for idx, (_, task_text, is_completed) in enumerate(tasks, 1):
            text += f"{'✅' if is_completed == 1 else '❌'} {idx}. {task_text}\n"
    await message.answer(text, parse_mode="Markdown")

@router.message(F.text == "🔙 Назад в меню")
async def cmd_back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Возвращаемся в главное меню.", reply_markup=get_main_menu())

@router.message(F.text.in_({"📝 День", "📝 Неделя", "📝 Месяц"}))
@router.message(F.text.in_({"📝 День", "📝 Неделя", "📝 Месяц"}))
async def show_period_tasks(message: Message, state: FSMContext):
    period_map = {"📝 День": "day", "📝 Неделя": "week", "📝 Месяц": "month"}
    period_code = period_map[message.text]
    
    await state.update_data(current_period=period_code)
    tasks = await get_tasks(message.from_user.id, period_code)
    
    # Расчет процентов
    total = len(tasks)
    done = sum(1 for t in tasks if t[2] == 1)
    percent = int((done / total) * 100) if total > 0 else 0
    
    period_title = message.text.replace("📝 ", "")
    text = f"📋 **Твои цели на [{period_title}]:**\n"
    text += f"📊 Прогресс: **{percent}%** ({done} из {total})\n\n"
    
    if not tasks:
        text += "У тебя пока нет целей на этот период. Самое время добавить! ➕"
    else:
        for idx, (_, task_text, is_completed) in enumerate(tasks, 1):
            status_icon = "✅" if is_completed == 1 else "❌"
            text += f"{status_icon} {idx}. {task_text}\n"
            
    await message.answer(text, reply_markup=get_task_management_menu(), parse_mode="Markdown")

# А. УМНОЕ ДОБАВЛЕНИЕ ЦЕЛИ (МНОЖЕСТВЕННЫЙ ВВОД)
@router.message(F.text == "➕ Добавить цель")
async def request_task_text(message: Message, state: FSMContext):
    data = await state.get_data()
    if 'current_period' not in data:
        return await message.answer("Сначала выбери период (День/Неделя/Месяц)!")
        
    await message.answer(
        "Напиши текст твоей цели и отправь её мне.\n\n"
        "После отправки ты сможешь сразу написать следующую. Когда закончишь, просто нажми кнопку ниже 👇",
        reply_markup=get_stop_adding_menu()
    )
    await state.set_state(TaskStates.waiting_for_task_text)

@router.message(TaskStates.waiting_for_task_text, F.text == "📥 Это всё (завершить)")
async def stop_adding_tasks(message: Message, state: FSMContext):
    data = await state.get_data()
    period_code = data.get('current_period', 'day')
    
    await state.set_state(State(None))
    tasks = await get_tasks(message.from_user.id, period_code)
    
    # Расчет процентов
    total = len(tasks)
    done = sum(1 for t in tasks if t[2] == 1)
    percent = int((done / total) * 100) if total > 0 else 0
    
    period_names = {"day": "День", "week": "Неделя", "month": "Месяц"}
    text = f"📋 **Твои цели на [{period_names[period_code]}]:**\n"
    text += f"📊 Прогресс: **{percent}%** ({done} из {total})\n\n"
    
    if not tasks:
        text += "У тебя пока нет целей на этот период. Самое время добавить! ➕"
    else:
        for idx, (_, task_text, is_completed) in enumerate(tasks, 1):
            status_icon = "✅" if is_completed == 1 else "❌"
            text += f"{status_icon} {idx}. {task_text}\n"
            
    await message.answer(text, reply_markup=get_task_management_menu(), parse_mode="Markdown")

@router.message(TaskStates.waiting_for_task_text)
async def process_add_task_loop(message: Message, state: FSMContext):
    data = await state.get_data()
    period_code = data['current_period']
    
    await add_task(message.from_user.id, message.text, period_code)
    await message.answer(
        f"🎯 Цель «{message.text}» успешно добавлена!\n\n"
        "Жду следующую цель. Если целей больше нет — нажми кнопку «📥 Это всё (завершить)»"
    )


# Б. ВЫПОЛНЕНИЕ ЦЕЛИ (С АВТОПЕРЕХОДОМ В СТАТИСТИКУ ФОКУСОВ)
@router.message(F.text == "✅ Выполнить цель")
async def select_task_to_complete(message: Message, state: FSMContext):
    data = await state.get_data()
    if 'current_period' not in data: return
    period_code = data['current_period']
    
    tasks = await get_tasks(message.from_user.id, period_code)
    uncompleted_tasks = [t for t in tasks if t[2] == 0]
    
    if not uncompleted_tasks:
        return await message.answer("У тебя нет невыполненных целей в этом периоде! Отличная работа! 🔥")
        
    gender = await get_user_gender(message.from_user.id)
    question = "Какую цель ты выполнил?" if gender == "male" else "Какую цель ты выполнила?"
    
    builder = InlineKeyboardBuilder()
    for task_id, task_text, _ in uncompleted_tasks:
        builder.button(text=f"❌ {task_text}", callback_data=f"comp_{task_id}")
    builder.button(text="🏁 Готово", callback_data="refresh_tasks")
    builder.adjust(1)
    
    await message.answer(question, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("comp_"))
async def process_complete_click(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    task_text = ""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT task_text FROM tasks WHERE task_id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
            if row: task_text = row[0]

    await complete_task_db(task_id)
    
    # Сканируем ключевые слова фокусов
    if task_text:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT focus_text FROM user_focuses WHERE user_id = ?", (user_id,)) as cursor:
                focuses = await cursor.fetchall()
                for (f_text,) in focuses:
                    if f_text.lower() in task_text.lower():
                        today = datetime.date.today().isoformat()
                        await db.execute("""
                            INSERT OR IGNORE INTO weekly_stats (user_id, date, focus_text) 
                            VALUES (?, ?, ?)
                        """, (user_id, today, f_text))
                        await db.commit()
                        break

    data = await state.get_data()
    period_code = data.get('current_period', 'day')
    tasks = await get_tasks(user_id, period_code)
    uncompleted_tasks = [t for t in tasks if t[2] == 0]
    
    builder = InlineKeyboardBuilder()
    for t_id, t_text, _ in uncompleted_tasks:
        builder.button(text=f"❌ {t_text}", callback_data=f"comp_{t_id}")
    builder.button(text="🏁 Готово", callback_data="refresh_tasks")
    builder.adjust(1)
    
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer("Цель засчитана! ✅")

@router.callback_query(F.data == "refresh_tasks")
async def finish_task_interaction(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    data = await state.get_data()
    period_code = data.get('current_period', 'day')
    
    period_names = {"day": "📝 День", "week": "📝 Неделя", "month": "📝 Месяц"}
    fake_msg = callback.message
    fake_msg.text = period_names[period_code]
    fake_msg.from_user.id = callback.from_user.id
    await show_period_tasks(fake_msg, state)
    await callback.answer()


# В. УДАЛЕНИЕ ЦЕЛИ
@router.message(F.text == "🗑 Удалить цель")
async def select_task_to_delete(message: Message, state: FSMContext):
    data = await state.get_data()
    if 'current_period' not in data: return
    period_code = data['current_period']
    
    tasks = await get_tasks(message.from_user.id, period_code)
    if not tasks:
        return await message.answer("Тут пока нечего удалять. Список пуст!")
        
    builder = InlineKeyboardBuilder()
    for task_id, task_text, is_comp in tasks:
        icon = "✅" if is_comp == 1 else "❌"
        builder.button(text=f"{icon} {task_text}", callback_data=f"del_{task_id}")
    builder.button(text="🏁 Готово", callback_data="refresh_tasks")
    builder.adjust(1)
    
    await message.answer("Нажми на цель, которую хочешь безвозвратно удалить:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("del_"))
async def process_delete_click(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split("_")[1])
    await delete_task_db(task_id)
    
    data = await state.get_data()
    period_code = data.get('current_period', 'day')
    tasks = await get_tasks(callback.from_user.id, period_code)
    
    builder = InlineKeyboardBuilder()
    for t_id, t_text, is_comp in tasks:
        icon = "✅" if is_comp == 1 else "❌"
        builder.button(text=f"{icon} {t_text}", callback_data=f"del_{t_id}")
    builder.button(text="🏁 Готово", callback_data="refresh_tasks")
    builder.adjust(1)
    
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer("Цель удалена 🗑")


# ==================== БЛОК 4: НАСТРОЙКА ФОКУСОВ И СТАТИСТИКА ====================

@router.message(F.text == "⚙️ Настройка Фокусов")
async def cmd_focus_settings(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT focus_text FROM user_focuses WHERE user_id = ?", (user_id,)) as cursor:
            focuses = await cursor.fetchall()
            
    text = "⚙️ **Твои фокусы (привычки) на неделю:**\n\n"
    if not focuses:
        text += "Ты еще не выбрал фокусы. Бот не собирает статистику! Нажми кнопку ниже, чтобы добавить ключевое слово (например: бег, кодинг, книги)."
    else:
        for idx, (f_text,) in enumerate(focuses, 1):
            text += f"🎯 {idx}. {f_text.capitalize()}\n"
            
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить фокус", callback_data="focus_add")
    if focuses:
        builder.button(text="🗑 Удалить фокус", callback_data="focus_del_menu")
    builder.adjust(1)
    
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@router.callback_query(F.data == "focus_add")
async def prompt_add_focus(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите одно ключевое слово для отслеживания (например: `бег`):", parse_mode="Markdown")
    await state.set_state(FocusStates.waiting_for_focus_text)
    await callback.answer()

@router.message(FocusStates.waiting_for_focus_text)
async def process_add_focus(message: Message, state: FSMContext):
    keyword = message.text.strip().lower()
    user_id = message.from_user.id
    
    # 1. Сохраняем в локальную БД
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO user_focuses (user_id, focus_text) VALUES (?, ?)", (user_id, keyword))
        await db.commit()
    
    # 2. Бекап в Google Таблицу
    try:
        agcm_client = await agcm.authorize()
        spreadsheet = await agcm_client.open("BotData")
        
        # Берем первый лист (индекс 0) для задач, а если хочешь отдельный для фокусов — 
        # создай в таблице второй лист и убедись, что он называется "Focuses"
        try:
            focus_sheet = await spreadsheet.worksheet("Focuses")
        except:
            focus_sheet = await spreadsheet.get_worksheet(0) # Если листа "Focuses" нет, пишет в первый
            
        await focus_sheet.append_row([user_id, keyword, datetime.datetime.now().isoformat()])
        logging.info(f"Фокус '{keyword}' записан в Google Таблицу")
    except Exception as e:
        logging.error(f"Не удалось записать фокус в таблицу: {e}")
        
    await state.clear()
    await message.answer(f"Ключевое слово «{keyword}» успешно добавлено в фокусы!")
    await cmd_focus_settings(message, state)

# ==========================================================
# ДУХОВНАЯ ПОДДЕРЖКА (ПОЛНЫЙ БЛОК)
# ==========================================================

# 1. Функция клавиатуры (должна быть в разделе КЛАВИАТУРЫ, 
# но если ты её не нашел, вставь её прямо здесь перед обработчиками)
def get_spiritual_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Лень"), KeyboardButton(text="Упадок сил")],
            [KeyboardButton(text="Тревога"), KeyboardButton(text="Потеря веры")],
            [KeyboardButton(text="🔙 Назад в меню")]
        ],
        resize_keyboard=True
    )

# 2. База стихов
SPIRITUAL_POEMS = {
    "Лень": [
        "Притчи 6:6: «Пойди к муравью, ленивец, посмотри на действия его, и будь мудр».",
        "Екклесиаст 9:10: «Все, что может рука твоя делать, по силам делай; потому что в могиле, куда ты пойдешь, нет ни работы, ни размышления, ни знания, ни мудрости».",
        "Притчи 13:4: «Душа ленивого желает, но тщетно; а душа прилежных насытится».",
        "Римлянам 12:11: «В усердии не ослабевайте; духом пламенейте; Господу служите».",
        "Притчи 10:4: «Ленивая рука делает бедным, а рука прилежных обогащает».",
        "Притчи 20:4: «Ленивец по зиме не пашет: поищет осенью — и нет ничего».",
        "Колоссянам 3:23: «И все, что делаете, делайте от души, как для Господа, а не для человеков».",
        "Притчи 12:24: «Рука прилежных будет господствовать, а ленивая будет под данью».",
        "2 Фессалоникийцам 3:10: «Если кто не хочет трудиться, тот и не ешь».",
        "Притчи 26:15: «Ленивец опускает руку свою в чашу, и ему тяжело донести ее до рта своего»."
    ],
    "Упадок сил": [
        "Исаия 40:31: «А надеющиеся на Господа обновятся в силе: поднимут крылья, как орлы, потекут — и не устанут, пойдут — и не утомятся».",
        "Галатам 6:9: «Делая добро, да не унываем, ибо в свое время пожнем, если не ослабеем».",
        "Филиппийцам 4:13: «Все могу в укрепляющем меня Иисусе Христе».",
        "2 Коринфянам 12:9: «Но Господь сказал мне: 'довольно для тебя благодати Моей, ибо сила Моя совершается в немощи'».",
        "Псалом 28:11: «Господь даст силу народу Своему, Господь благословит народ Свой миром».",
        "Матфея 11:28: «Придите ко Мне все труждающиеся и обремененные, и Я успокою вас».",
        "Исаия 41:13: «Ибо Я — Господь, Бог твой; держу тебя за правую руку твою, говоря тебе: 'не бойся, Я помогаю тебе'».",
        "Псалом 45:2: «Бог нам прибежище и сила, скорый помощник в бедах».",
        "1 Петра 5:7: «Все заботы ваши возложите на Него, ибо Он печется о вас».",
        "Псалом 17:33: «Бог препоясывает меня силою и устрояет мне верный путь»."
    ],
    "Тревога": [
        "Филиппийцам 4:6–7: «Не заботьтесь ни о чем, но всегда в молитве и прошении с благодарением открывайте свои желания пред Богом».",
        "Исаия 41:10: «Не бойся, ибо Я с тобою; не смущайся, ибо Я Бог твой; Я укреплю тебя, и помогу тебе».",
        "Матфея 6:34: «Итак не заботьтесь о завтрашнем дне, ибо завтрашний сам будет заботиться о своем: довольно для каждого дня своей заботы».",
        "Псалом 93:19: «При умножении скорбей моих в сердце моем, утешения Твои услаждают душу мою».",
        "Матфея 10:31: «Не бойтесь же: вы лучше многих малых птиц».",
        "Иоанна 14:27: «Мир оставляю вам, мир Мой даю вам; не так, как мир дает, Я даю вам. Да не смущается сердце ваше и да не устрашается».",
        "Псалом 55:4: «Когда я в страхе, на Тебя я уповаю».",
        "Иисус Навин 1:9: «Будь тверд и мужествен, не страшись и не ужасайся; ибо с тобою Господь Бог твой везде, куда ни пойдешь».",
        "Притчи 3:5: «Надейся на Господа всем сердцем твоим, и не полагайся на разум твой».",
        "Псалом 33:5: «Я взыскал Господа, и Он услышал меня, и от всех опасностей моих избавил меня»."
    ],
    "Потеря веры": [
        "Притчи 24:16: «Ибо семь раз упадет праведник, и встанет; а нечестивые впадут в погибель».",
        "Иеремия 29:11: «Ибо Я знаю намерения, какие имею о вас, говорит Господь, намерения во благо, а не на зло, чтобы дать вам будущность и надежду».",
        "Исаия 43:2: «Будешь ли переходить через воды, Я с тобою, — через реки ли, они не потопят тебя; пойдешь ли через огонь, не обожжешься».",
        "Псалом 146:3: «Он исцеляет сокрушенных сердцем и врачует скорби их».",
        "Марка 9:23: «Иисус сказал ему: если сколько-нибудь можешь веровать, все возможно верующему».",
        "Евреям 11:1: «Вера же есть осуществление ожидаемого и уверенность в невидимом».",
        "Римлянам 8:28: «Притом знаем, что любящим Бога... все содействует ко благу».",
        "Иоанна 16:33: «В мире будете иметь скорбь; но мужайтесь: Я победил мир».",
        "Псалом 118:105: «Слово Твое — светильник ноге моей и свет стезе моей».",
        "Притчи 18:10: «Имя Господа — крепкая башня: убегает в нее праведник — и безопасен»."
    ]
}

# 3. Обработчики
@router.message(F.text == "🛡 Духовная поддержка")
async def cmd_spiritual_support(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Я рядом. Что тебя сейчас беспокоит?", reply_markup=get_spiritual_menu())

@router.message(F.text.in_({"Лень", "Упадок сил", "Тревога", "Потеря веры"}))
async def spiritual_response(message: Message):
    category = message.text
    poem = random.choice(SPIRITUAL_POEMS.get(category, ["Стихов пока нет."]))
    await message.answer(f"📖 **{category}**:\n\n{poem}")

@router.message(F.text == "🔙 Назад в меню")
async def cmd_back_to_main_from_spirit(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Возвращаемся в главное меню.", reply_markup=get_main_menu())

@router.callback_query(F.data == "focus_del_menu")
async def show_focus_delete_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT focus_text FROM user_focuses WHERE user_id = ?", (user_id,)) as cursor:
            focuses = await cursor.fetchall()
            
    builder = InlineKeyboardBuilder()
    for (f_text,) in focuses:
        builder.button(text=f"🗑 {f_text.capitalize()}", callback_data=f"fdel_{f_text}")
    builder.button(text="🏁 Готово", callback_data="focus_refresh")
    builder.adjust(1)
    
    await callback.message.edit_text("Нажми на фокус, который хочешь удалить:", reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("fdel_"))
async def process_focus_delete(callback: CallbackQuery):
    focus_text = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_focuses WHERE user_id = ? AND focus_text = ?", (user_id, focus_text))
        await db.commit()
        async with db.execute("SELECT focus_text FROM user_focuses WHERE user_id = ?", (user_id,)) as cursor:
            focuses = await cursor.fetchall()
            
    builder = InlineKeyboardBuilder()
    for (f_text,) in focuses:
        builder.button(text=f"🗑 {f_text.capitalize()}", callback_data=f"fdel_{f_text}")
    builder.button(text="🏁 Готово", callback_data="focus_refresh")
    builder.adjust(1)
    
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer(f"Фокус '{focus_text}' удален")

@router.callback_query(F.data == "focus_refresh")
async def refresh_focus_menu_click(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    fake_msg = callback.message
    fake_msg.from_user.id = callback.from_user.id
    await cmd_focus_settings(fake_msg, state)


# ==================== ВОСКРЕСНЫЕ ОТЧЕТЫ (КНОПКИ ДА/НЕТ) ====================

@router.callback_query(F.data == "view_weekly_stats")
async def show_weekly_stats_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT focus_text FROM user_focuses WHERE user_id = ?", (user_id,)) as cursor:
            focuses = await cursor.fetchall()
            
        text = "📊 **Твои результаты за неделю:**\n\n"
        if not focuses:
            text += "У тебя не было настроенных фокусов на этой неделе!"
        else:
            for (f_text,) in focuses:
                async with db.execute(
                    "SELECT COUNT() FROM weekly_stats WHERE user_id = ? AND focus_text = ?", 
                    (user_id, f_text)
                ) as count_cursor:
                    row = await count_cursor.fetchone()
                    count = row[0] if row else 0
                text += f"🎯 **{f_text.capitalize()}**: {count} из 7 дней\n"
                
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "dismiss_stats")
async def dismiss_stats_callback(callback: CallbackQuery):
    await callback.message.edit_text("Хорошо! Если захочешь посмотреть, я всегда здесь. Продуктивной новой недели! 🔥")
    await callback.answer()


# ==================== АВТОНОМНЫЙ ПЛАНИРОВЩИК ВРЕМЕНИ ====================

async def send_sunday_report_prompt(bot: Bot):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM user_focuses") as cursor:
            users = await cursor.fetchall()
            
    for (user_id,) in users:
        try:
            builder = InlineKeyboardBuilder()
            builder.button(text="Да ✅", callback_data="view_weekly_stats")
            builder.button(text="Нет ❌", callback_data="dismiss_stats")
            builder.adjust(2)
            await bot.send_message(
                user_id, 
                "Неделя подошла к концу! Хочешь посмотреть свою статистику продуктивности за прошедшие дни?", 
                reply_markup=builder.as_markup()
            )
        except Exception:
            pass

async def midnight_reset_job():
    """Сброс в 00:00: День (каждый день), Неделя (по ПН), Месяц (1-го числа)"""
    today = datetime.date.today()
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Сброс планера Дня — выполняется каждую ночь беспрекословно
        await db.execute("DELETE FROM tasks WHERE period = 'day'")
        
        # 2. Сброс планера Недели и статистики фокусов — только в полночь понедельника
        if today.weekday() == 0:
            await db.execute("DELETE FROM tasks WHERE period = 'week'")
            await db.execute("DELETE FROM weekly_stats")
            print("Выполнен плановый сброс недельных целей и статистики фокусов.")
            
        # 3. Сброс планера Месяца — только в полночь 1-го числа каждого месяца
        if today.day == 1:
            await db.execute("DELETE FROM tasks WHERE period = 'month'")
            print("Выполнен плановый сброс месячных целей.")
            
        await db.commit()
    print("Автономный полуночный сброс отработал успешно.")


# ==================== ЗАПУСК БОТА ====================
async def main():
    print("🤖 Инициализируем базу данных...")
    await init_db()
    
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
    
    # 1. Будильник на полночь: Очищает периоды согласно календарю
    scheduler.add_job(midnight_reset_job, 'cron', hour=0, minute=0)
    
    # 2. Будильник на отчеты: Срабатывает строго по Воскресеньям в 21:00
    scheduler.add_job(send_sunday_report_prompt, 'cron', day_of_week='sun', hour=21, minute=0, args=[bot])
    
    scheduler.start()
    print("⏰ Фоновые таймеры (Minsk Time) успешно запущены в боевом режиме!")
    
    # ВАЖНО: Добавляем эту строчку перед стартом поллинга
    await bot.delete_webhook(drop_pending_updates=True)
    
    print("🚀 Бот полностью готов к работе!")
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    async def run_with_server():
        # Запускаем микро-сервер для Render
        from aiohttp import web
        import os
        app = web.Application()
        app.router.add_get('/', lambda r: web.Response(text="Bot is alive"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080)))
        await site.start()
        
        # Запускаем самого бота
        await main()

    import asyncio
    asyncio.run(run_with_server())
