import os
import logging
import asyncio
import tempfile
import traceback
import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.enums import ParseMode
from aiogram.types import Message, FSInputFile
from openai import AsyncOpenAI
from dotenv import load_dotenv
from docx import Document
from redis.asyncio import Redis, RedisError, ConnectionError as RedisConnectionError

# Очистка переменных окружения прокси
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("ALL_PROXY", None)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

# Валидация обязательных переменных
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
REDIS_URL = os.getenv("REDIS_URL")

if not all([BOT_TOKEN, OPENAI_API_KEY, REDIS_URL]):
    raise EnvironmentError(
        "Не заданы обязательные переменные окружения: "
        "BOT_TOKEN, OPENAI_API_KEY, REDIS_URL"
    )

# Инициализация OpenAI клиента
openai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    http_client=httpx.AsyncClient(
        proxies=None,
        timeout=30.0,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=2
        )
    )
)

# Инициализация Redis
try:
    redis_client = Redis.from_url(
        REDIS_URL,
        ssl=True,
        ssl_cert_reqs=None,
        socket_timeout=10,
        socket_connect_timeout=5,
        retry_on_timeout=True,
        decode_responses=True,
        health_check_interval=30
    )
    storage = RedisStorage(redis=redis_client)
except (RedisError, RedisConnectionError) as e:
    logger.critical(f"Redis connection error: {str(e)}")
    raise

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=storage)

# Состояния FSM
class DocGenState(StatesGroup):
    waiting_for_initial_input = State()
    waiting_for_special_terms = State()

# Проверка подключения Redis
async def check_redis_connection():
    try:
        if await redis_client.ping():
            logger.info("✅ Redis connection verified")
            return True
        logger.error("❌ Redis ping failed")
        return False
    except Exception as e:
        logger.critical(f"Redis connection failed: {str(e)}")
        raise

# Генерация документов через OpenAI
async def generate_gpt_response(system_prompt: str, user_prompt: str) -> str:
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=3000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}\n{traceback.format_exc()}")
        return "❌ Произошла ошибка при генерации документа. Попробуйте позже."

# Работа с DOCX файлами
def save_docx(text: str, filename: str) -> str:
    try:
        doc = Document()
        for para in text.split("\n"):
            if para.strip():
                doc.add_paragraph(para)
        
        temp_dir = tempfile.gettempdir()
        filepath = os.path.join(temp_dir, filename)
        doc.save(filepath)
        return filepath
    except Exception as e:
        logger.error(f"DOCX creation error: {e}\n{traceback.format_exc()}")
        raise

async def safe_send_document(message: Message, path: str):
    try:
        await message.answer_document(FSInputFile(path))
    finally:
        if os.path.exists(path):
            try:
                os.unlink(path)
            except Exception as e:
                logger.warning(f"File deletion error {path}: {e}")

# Обработчики команд
@dp.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    try:
        await state.clear()
        await message.answer(
            "👋 Привет! Я помогу составить юридический документ.\n\n"
            "Просто опиши, какой документ тебе нужен. Например:\n"
            "<i>Нужен договор аренды офиса между ИП и ООО на год</i>"
        )
        await state.set_state(DocGenState.waiting_for_initial_input)
    except Exception as e:
        logger.error(f"/start error: {e}\n{traceback.format_exc()}")
        await message.answer("⚠️ Произошла внутренняя ошибка. Попробуйте позже.")

@dp.message(DocGenState.waiting_for_initial_input)
async def handle_description(message: Message, state: FSMContext):
    try:
        if len(message.text) > 3000:
            await message.answer("⚠️ Слишком длинный текст. Укороти, пожалуйста.")
            return

        await state.update_data(initial_text=message.text)
        await message.answer("🧠 Генерирую черновик документа...")

        document = await generate_gpt_response(
            system_prompt="Ты опытный юрист. Составь юридически корректный документ.",
            user_prompt=f"Составь юридический документ по российскому праву. Вот описание от пользователя:\n\n\"{message.text}\""
        )

        filename = f"draft_{message.from_user.id}.docx"
        path = save_docx(document, filename)
        
        await state.update_data(document_text=document)
        await message.answer("📄 Вот сгенерированный документ:")
        await safe_send_document(message, path)
        await message.answer("Хочешь добавить особые условия? Напиши их или напиши <b>нет</b>.")
        await state.set_state(DocGenState.waiting_for_special_terms)
        
    except Exception as e:
        logger.error(f"Description processing error: {e}\n{traceback.format_exc()}")
        await message.answer("⚠️ Произошла ошибка при обработке запроса. Попробуйте снова.")
        await state.clear()

@dp.message(DocGenState.waiting_for_special_terms)
async def handle_additions(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        base_text = data.get("document_text", "")

        if message.text.strip().lower() == "нет":
            await message.answer("✅ Документ завершён. Удачи!")
            await state.clear()
            return

        await message.answer("🔧 Вношу изменения...")
        updated_doc = await generate_gpt_response(
            system_prompt="Ты юридический редактор. Вноси только необходимые правки, сохраняя стиль.",
            user_prompt=(
                "Вот документ. Добавь в него аккуратно следующие особые условия, "
                f"сохранив стиль и структуру:\n\nУсловия: {message.text}\n\nДокумент:\n{base_text}"
            )
        )

        filename = f"final_{message.from_user.id}.docx"
        path = save_docx(updated_doc, filename)
        
        await message.answer("📄 Документ с учётом условий:")
        await safe_send_document(message, path)
        await message.answer("✅ Готово!")
        await state.clear()
        
    except Exception as e:
        logger.error(f"Additions processing error: {e}\n{traceback.format_exc()}")
        await message.answer("⚠️ Произошла ошибка при обработке условий. Попробуйте снова.")
        await state.clear()

# Запуск приложения
if __name__ == "__main__":
    try:
        logger.info("Starting bot...")
        asyncio.run(check_redis_connection())
        asyncio.run(dp.start_polling(bot))
    except KeyboardInterrupt:
        logger.info("Bot stopped gracefully")
    except Exception as e:
        logger.critical(f"Fatal error: {str(e)}\n{traceback.format_exc()}")