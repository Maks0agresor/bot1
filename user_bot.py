import os
import logging
import secrets
import motor.motor_asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from dotenv import load_dotenv
import time
import asyncio
from datetime import datetime
from pymongo.errors import OperationFailure

# Загрузка переменных окружения
load_dotenv()

TOKEN = os.getenv("USER_BOT_TOKEN")  # Токен бота для пользователей
MONGO_URI = os.getenv("MONGO_URI")

# Идентификатор вашего канала
CHANNEL_ID = '@exchange_CMM'  # Замените на @username вашего канала

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

# Роутер для регистрации хэндлеров
router = Router()

# Генерация уникального токена
def generate_token() -> str:
    return secrets.token_urlsafe(16)

# Ограничение количества запросов от пользователя
user_last_token_request = {}

async def rate_limit(user_id: int, limit_time: int = 120) -> bool:
    last_request_time = user_last_token_request.get(user_id)
    current_time = int(time.time())

    if last_request_time and current_time - last_request_time < limit_time:
        return False
    user_last_token_request[user_id] = current_time
    return True

# FSM классы для обработки состояний
class DeleteTokenState(StatesGroup):
    waiting_for_token = State()

# Функция для проверки подписки пользователя
async def is_user_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        if member.status in ['creator', 'administrator', 'member']:
            return True
        else:
            return False
    except Exception as e:
        logger.error(f"Ошибка при проверке подписки пользователя: {e}")
        return False

# Хэндлер команды /start
@router.message(F.text == "/start")
async def start(message: types.Message):
    user_id = message.from_user.id
    user = await users_collection.find_one({"user_id": user_id})
    if not user:
        await users_collection.insert_one({"user_id": user_id, "joined_at": message.date})

    # Создаем клавиатуру для пользователей
    markup = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Ключи"), KeyboardButton(text="Стереть ключ")],
            [KeyboardButton(text="Наш канал")]
        ],
        resize_keyboard=True,
    )

    await message.answer("Я ваш помощник в обмене файлами.", reply_markup=markup)

# Хэндлер для кнопки "Наш канал"
@router.message(F.text == "Наш канал")
async def send_channel_info(message: types.Message):
    await message.answer(
        "Подписывайтесь на наш канал, чтобы быть в курсе новостей: @your_channel_username"
    )

# Хэндлер для команды 'Ключи'
@router.message(F.text == "Ключи")
async def list_user_tokens(message: types.Message):
    user_id = message.from_user.id

    # Проверка на лимит запросов
    if not await rate_limit(user_id):
        await message.answer("Подождите немного перед следующим запросом (60 сек.)")
        return

    tokens_cursor = files_collection.find(
        {"user_id": user_id}, {"_id": 0, "token": 1, "users": 1}
    )
    tokens = await tokens_cursor.to_list(length=None)

    if not tokens:
        await message.answer("У вас нет сохраненных ключей.")
        return

    # Использование Markdown для удобного копирования токенов
    token_list = [
        f'`{token["token"]}` — Использований: {len(token.get("users", []))}'
        for token in tokens
    ]
    message_chunks = [token_list[i : i + 10] for i in range(0, len(token_list), 10)]

    for chunk in message_chunks:
        await message.answer("\n".join(chunk), parse_mode="Markdown")

# Хэндлер команды 'Стереть ключ'
@router.message(F.text == "Стереть ключ")
async def start_user_token_deletion(message: types.Message, state: FSMContext):
    await message.answer("Отправьте токен, который хотите стереть.")
    await state.set_state(DeleteTokenState.waiting_for_token)

@router.message(DeleteTokenState.waiting_for_token)
async def process_user_token_deletion(message: types.Message, state: FSMContext):
    token = message.text.strip()
    user_id = message.from_user.id

    file_doc = await files_collection.find_one({"token": token, "user_id": user_id})
    if file_doc:
        await files_collection.delete_one({"token": token, "user_id": user_id})
        await message.answer(f"Ключ {token} был успешно стерт.")
    else:
        await message.answer("Ключ не найден или не принадлежит вам.")

    await state.clear()

# Обработчик загрузки файла (документ)
@router.message(F.content_type == "document")
async def handle_file(message: types.Message):
    user_id = message.from_user.id

    # Проверка подписки
    is_subscribed = await is_user_subscribed(user_id)
    if not is_subscribed:
        await message.answer(
            "Чтобы быть в курсе новостей и получать обновления, подпишитесь на наш канал: @your_channel_username"
        )

    # Получаем информацию о файле через метод get_file
    file_info = await bot.get_file(message.document.file_id)
    file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

    token = generate_token()

    await files_collection.insert_one(
        {
            "token": token,
            "file_id": message.document.file_id,
            "file_url": file_url,
            "user_id": user_id,
            "uploaded_at": datetime.utcnow(),
            "file_type": "document",
            "users": [],
        }
    )

    await message.answer(
        f"Файл сохранен. Можете поделиться им, просто отправьте этот ключ боту: `{token}`",
        parse_mode="Markdown",
    )

# Обработчик сжатых фото
@router.message(F.content_type == "photo")
async def handle_photo(message: types.Message):
    user_id = message.from_user.id

    # Проверка подписки
    is_subscribed = await is_user_subscribed(user_id)
    if not is_subscribed:
        await message.answer(
            "Чтобы быть в курсе новостей и получать обновления, подпишитесь на наш канал: @your_channel_username"
        )

    # Берем самое большое фото
    file_id = message.photo[-1].file_id

    # Получаем информацию о фото через метод get_file
    file_info = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

    token = generate_token()

    await files_collection.insert_one(
        {
            "token": token,
            "file_id": file_id,
            "file_url": file_url,
            "user_id": user_id,
            "uploaded_at": datetime.utcnow(),
            "file_type": "photo",
            "users": [],
        }
    )

    await message.answer(
        f"Фото сохранено. Можете поделиться им, просто отправьте этот ключ боту: `{token}`",
        parse_mode="Markdown",
    )

# Обработчик сжатых видео
@router.message(F.content_type == "video")
async def handle_video(message: types.Message):
    user_id = message.from_user.id

    # Проверка подписки
    is_subscribed = await is_user_subscribed(user_id)
    if not is_subscribed:
        await message.answer(
            "Чтобы быть в курсе новостей и получать обновления, подпишитесь на наш канал: @your_channel_username"
        )

    file_id = message.video.file_id

    # Получаем информацию о видео через метод get_file
    file_info = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"

    token = generate_token()

    await files_collection.insert_one(
        {
            "token": token,
            "file_id": file_id,
            "file_url": file_url,
            "user_id": user_id,
            "uploaded_at": datetime.utcnow(),
            "file_type": "video",
            "users": [],
        }
    )

    await message.answer(
        f"Видео сохранено. Можете поделиться им, просто отправьте этот ключ боту: `{token}`",
        parse_mode="Markdown",
    )

# Обработчик текста (обработка токена и загрузка файла)
@router.message(F.content_type == "text")
async def handle_text_message(message: types.Message):
    user_id = message.from_user.id

    # Проверка токена
    token = message.text.strip()

    file_doc = await files_collection.find_one({"token": token})
    if file_doc:
        # Проверка подписки
        is_subscribed = await is_user_subscribed(user_id)
        if not is_subscribed:
            await message.answer(
                "Чтобы быть в курсе новостей и получать обновления, подпишитесь на наш канал: @your_channel_username"
            )

        if user_id not in file_doc.get("users", []):
            await files_collection.update_one(
                {"token": token}, {"$addToSet": {"users": user_id}}
            )

        try:
            if file_doc["file_type"] == "photo":
                await bot.send_photo(message.chat.id, file_doc["file_id"])
            elif file_doc["file_type"] == "video":
                await bot.send_video(message.chat.id, file_doc["file_id"])
            else:
                await bot.send_document(message.chat.id, file_doc["file_id"])
        except Exception as e:
            logger.error(f"Ошибка при отправке файла через file_id: {e}")
            await message.answer("file_id больше недоступен, отправляю файл по ссылке.")
            await bot.send_message(message.chat.id, file_doc["file_url"])
    else:
        await message.answer("Файл с таким ключом не найден.")

# Функция для создания индексов в базе данных
async def create_indexes():
    # Получаем информацию о существующих индексах в 'files_collection'
    existing_indexes_files = await files_collection.index_information()

    # Обработка индекса на поле 'token' в коллекции 'files_collection'
    index_name = None
    for name, info in existing_indexes_files.items():
        keys = info.get('key')
        if keys and keys[0][0] == 'token':
            index_name = name
            break

    if index_name:
        # Удаляем существующий индекс на 'token'
        try:
            await files_collection.drop_index(index_name)
            logger.info(f"Существующий индекс '{index_name}' на поле 'token' был удален.")
        except OperationFailure as e:
            logger.error(f"Ошибка при удалении индекса '{index_name}': {e}")
            return

    # Создаем новый уникальный индекс на поле 'token'
    try:
        await files_collection.create_index('token', name='token_index', unique=True)
        logger.info("Индекс 'token_index' в 'files_collection' создан.")
    except OperationFailure as e:
        logger.error(f"Ошибка при создании индекса 'token_index': {e}")
        return

    # Аналогично для 'users_collection'
    existing_indexes_users = await users_collection.index_information()

    index_name = None
    for name, info in existing_indexes_users.items():
        keys = info.get('key')
        if keys and keys[0][0] == 'user_id':
            index_name = name
            break

    if index_name:
        # Удаляем существующий индекс на 'user_id'
        try:
            await users_collection.drop_index(index_name)
            logger.info(f"Существующий индекс '{index_name}' на поле 'user_id' был удален.")
        except OperationFailure as e:
            logger.error(f"Ошибка при удалении индекса '{index_name}': {e}")
            return

    # Создаем новый уникальный индекс на поле 'user_id' в 'users_collection'
    try:
        await users_collection.create_index('user_id', name='user_id_index', unique=True)
        logger.info("Индекс 'user_id_index' в 'users_collection' создан.")
    except OperationFailure as e:
        logger.error(f"Ошибка при создании индекса 'user_id_index': {e}")
        return

    # Создаем индексы на 'user_id', 'uploaded_at' в 'files_collection'
    try:
        await files_collection.create_index('user_id', name='files_user_id_index')
        logger.info("Индекс 'files_user_id_index' в 'files_collection' создан.")
    except OperationFailure as e:
        logger.error(f"Ошибка при создании индекса 'files_user_id_index': {e}")

    try:
        await files_collection.create_index('uploaded_at', name='uploaded_at_index')
        logger.info("Индекс 'uploaded_at_index' в 'files_collection' создан.")
    except OperationFailure as e:
        logger.error(f"Ошибка при создании индекса 'uploaded_at_index': {e}")

# Запуск бота
async def main():
    await create_indexes()  # Создаем индексы в базе данных
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
