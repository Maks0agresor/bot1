import os
import logging
import motor.motor_asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from dotenv import load_dotenv
import asyncio
from datetime import datetime, timedelta
from aiogram.types import FSInputFile
from functools import wraps

# Загрузка переменных окружения
load_dotenv()

TOKEN = os.getenv("ADMIN_BOT_TOKEN")  # Токен бота для администратора
MONGO_URI = os.getenv("MONGO_URI")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Асинхронное подключение к MongoDB
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client["telegram_bot_db"]
users_collection = db["users"]
files_collection = db["files"]

# Создание бота и диспетчера
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# Список ID администраторов
ADMIN_IDS = [1758717629]  # Замените на ваши реальные ID

# Декоратор для проверки прав администратора
def admin_only(handler):
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        message = kwargs.get('message')
        if not message:
            for arg in args:
                if isinstance(arg, types.Message):
                    message = arg
                    break
        if message and message.from_user.id in ADMIN_IDS:
            return await handler(*args, **kwargs)
        else:
            await message.answer("У вас нет прав для выполнения этой команды.")
    return wrapper

# FSM классы для обработки состояний
class AdminTokenStatsState(StatesGroup):
    waiting_for_token = State()

class AdminDeleteTokensState(StatesGroup):
    waiting_for_tokens = State()

# Хэндлер команды /start
@router.message(F.text == "/start")
@admin_only
async def start(message: types.Message, **kwargs):
    await message.answer("Добро пожаловать в админский бот. Используйте команды для управления.")

# Команда: Показать количество пользователей
@router.message(F.text == "/user_count")
@admin_only
async def user_count_handler(message: types.Message, **kwargs):
    user_count = await users_collection.count_documents({})
    await message.answer(f"Общее количество пользователей: {user_count}")

# Команда: Показать топ популярных токенов
@router.message(F.text.startswith("/top_tokens"))
@admin_only
async def top_tokens_handler(message: types.Message, **kwargs):
    try:
        parts = message.text.strip().split()
        if len(parts) == 2:
            top_n = int(parts[1])
        else:
            top_n = 10  # Значение по умолчанию
    except ValueError:
        await message.answer(
            "Пожалуйста, укажите число для количества топовых токенов. Пример: /top_tokens 10"
        )
        return

    pipeline = [
        {
            "$project": {
                "token": 1,
                "usage_count": {"$size": {"$ifNull": ["$users", []]}},
            }
        },
        {"$sort": {"usage_count": -1}},
        {"$limit": top_n},
    ]
    top_tokens = await files_collection.aggregate(pipeline).to_list(length=top_n)

    if not top_tokens:
        await message.answer("Нет данных о токенах.")
        return

    response = "Топ популярных токенов:\n"
    for token_info in top_tokens:
        response += f"Токен: `{token_info['token']}`, Использований: {token_info['usage_count']}\n"
    await message.answer(response, parse_mode="Markdown")

# Команда: Показать статистику токена
@router.message(F.text == "/token_stats")
@admin_only
async def token_stats_start(message: types.Message, state: FSMContext, **kwargs):
    await message.answer("Пожалуйста, отправьте токен, для которого хотите получить статистику.")
    await state.set_state(AdminTokenStatsState.waiting_for_token)

@router.message(AdminTokenStatsState.waiting_for_token)
@admin_only
async def token_stats_process(message: types.Message, state: FSMContext, **kwargs):
    token = message.text.strip()
    file_doc = await files_collection.find_one({"token": token})
    if file_doc:
        usage_count = len(file_doc.get("users", []))
        await message.answer(
            f"Токен `{token}` был использован {usage_count} раз(а) уникальными пользователями.",
            parse_mode="Markdown",
        )
    else:
        await message.answer("Токен не найден.")
    await state.clear()

# Команда: Удаление токенов
@router.message(F.text == "/delete_tokens")
@admin_only
async def delete_tokens_start(message: types.Message, state: FSMContext, **kwargs):
    await message.answer("Пожалуйста, отправьте токены, которые хотите удалить, разделенные пробелом.")
    await state.set_state(AdminDeleteTokensState.waiting_for_tokens)

@router.message(AdminDeleteTokensState.waiting_for_tokens)
@admin_only
async def delete_tokens_process(message: types.Message, state: FSMContext, **kwargs):
    tokens_text = message.text.strip()
    tokens = tokens_text.split()
    if not tokens:
        await message.answer("Вы не указали ни одного токена.")
        await state.clear()
        return

    delete_result = await files_collection.delete_many({"token": {"$in": tokens}})
    deleted_count = delete_result.deleted_count

    if deleted_count > 0:
        await message.answer(f"Успешно удалено токенов: {deleted_count}")
    else:
        await message.answer("Ни один из указанных токенов не был найден.")
    await state.clear()

# Команда: Получить файл со всеми токенами за последние 24 часа
@router.message(F.text == "/tokens_last24h")
@admin_only
async def tokens_last24h_handler(message: types.Message, **kwargs):
    now = datetime.utcnow()
    last_24h = now - timedelta(hours=24)

    tokens_cursor = files_collection.find(
        {"uploaded_at": {"$gte": last_24h}}, {"token": 1}
    )
    tokens = await tokens_cursor.to_list(length=None)
    if not tokens:
        await message.answer("За последние 24 часа не было загружено токенов.")
        return

    # Форматируем токены по 10 в строке
    tokens_list = [token["token"] for token in tokens]
    lines = []
    for i in range(0, len(tokens_list), 10):
        line = " ".join(tokens_list[i : i + 10])
        lines.append(line)

    # Создаем временный файл
    file_content = "\n".join(lines)
    file_name = "tokens_last24h.txt"
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(file_content)

    # Отправляем файл администратору
    await bot.send_document(message.chat.id, FSInputFile(file_name))

    # Удаляем временный файл
    os.remove(file_name)

# Запуск бота
async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
