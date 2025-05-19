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
from redis.asyncio import Redis

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()

class BotApplication:
    def __init__(self):
        self.bot = None
        self.dp = None
        self.redis = None
        self.openai_client = None
        self.states = None

    async def initialize(self):
        # Очистка переменных окружения прокси
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("ALL_PROXY", None)

        # Валидация переменных окружения
        BOT_TOKEN = os.getenv("BOT_TOKEN")
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        REDIS_URL = os.getenv("REDIS_URL")

        if not all([BOT_TOKEN, OPENAI_API_KEY, REDIS_URL]):
            raise EnvironmentError(
                "Не заданы обязательные переменные окружения: "
                "BOT_TOKEN, OPENAI_API_KEY, REDIS_URL"
            )

        # Инициализация OpenAI клиента
        self.openai_client = AsyncOpenAI(
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
        self.redis = Redis.from_url(
            REDIS_URL,
            socket_timeout=10,
            socket_connect_timeout=5,
            retry_on_timeout=True,
            decode_responses=True,
            health_check_interval=30
        )
        
        # Проверка подключения к Redis
        if not await self.redis.ping():
            raise ConnectionError("Не удалось подключиться к Redis")

        storage = RedisStorage(redis=self.redis)

        # Инициализация бота и диспетчера
        self.bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
        self.dp = Dispatcher(storage=storage)

        # Определение состояний FSM
        class DocGenState(StatesGroup):
            waiting_for_initial_input = State()
            waiting_for_special_terms = State()
        
        self.states = DocGenState

        # Регистрация обработчиков
        self.register_handlers()

    def register_handlers(self):
        @self.dp.message(F.text == "/start")
        async def cmd_start(message: Message, state: FSMContext):
            try:
                await state.clear()
                await message.answer(
                    "👋 Привет! Я помогу составить юридический документ.\n\n"
                    "Просто опиши, какой документ тебе нужен. Например:\n"
                    "<i>Нужен договор аренды офиса между ИП и ООО на год</i>"
                )
                await state.set_state(self.states.waiting_for_initial_input)
            except Exception as e:
                logger.error(f"Ошибка в /start: {e}\n{traceback.format_exc()}")
                await message.answer("⚠️ Произошла внутренняя ошибка. Попробуйте позже.")

        @self.dp.message(self.states.waiting_for_initial_input)
        async def handle_description(message: Message, state: FSMContext):
            try:
                if len(message.text) > 3000:
                    await message.answer("⚠️ Слишком длинный текст. Укороти, пожалуйста.")
                    return

                await state.update_data(initial_text=message.text)
                await message.answer("🧠 Генерирую черновик документа...")

                document = await self.generate_gpt_response(
                    system_prompt="Ты опытный юрист. Составь юридически корректный документ.",
                    user_prompt=f"Составь юридический документ по российскому праву. Вот описание от пользователя:\n\n\"{message.text}\""
                )

                filename = f"draft_{message.from_user.id}.docx"
                path = self.save_docx(document, filename)
                
                await state.update_data(document_text=document)
                await message.answer("📄 Вот сгенерированный документ:")
                await self.safe_send_document(message, path)
                await message.answer("Хочешь добавить особые условия? Напиши их или напиши <b>нет</b>.")
                await state.set_state(self.states.waiting_for_special_terms)
                
            except Exception as e:
                logger.error(f"Ошибка обработки описания: {e}\n{traceback.format_exc()}")
                await message.answer("⚠️ Произошла ошибка при обработке запроса. Попробуйте снова.")
                await state.clear()

        @self.dp.message(self.states.waiting_for_special_terms)
        async def handle_additions(message: Message, state: FSMContext):
            try:
                data = await state.get_data()
                base_text = data.get("document_text", "")

                if message.text.strip().lower() == "нет":
                    await message.answer("✅ Документ завершён. Удачи!")
                    await state.clear()
                    return

                await message.answer("🔧 Вношу изменения...")
                updated_doc = await self.generate_gpt_response(
                    system_prompt="Ты юридический редактор. Вноси только необходимые правки, сохраняя стиль.",
                    user_prompt=(
                        "Вот документ. Добавь в него аккуратно следующие особые условия, "
                        f"сохранив стиль и структуру:\n\nУсловия: {message.text}\n\nДокумент:\n{base_text}"
                    )
                )

                filename = f"final_{message.from_user.id}.docx"
                path = self.save_docx(updated_doc, filename)
                
                await message.answer("📄 Документ с учётом условий:")
                await self.safe_send_document(message, path)
                await message.answer("✅ Готово!")
                await state.clear()
                
            except Exception as e:
                logger.error(f"Ошибка обработки условий: {e}\n{traceback.format_exc()}")
                await message.answer("⚠️ Произошла ошибка при обработке условий. Попробуйте снова.")
                await state.clear()

    async def generate_gpt_response(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = await self.openai_client.chat.completions.create(
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
            logger.error(f"Ошибка OpenAI: {e}\n{traceback.format_exc()}")
            return "❌ Произошла ошибка при генерации документа. Попробуйте позже."

    def save_docx(self, text: str, filename: str) -> str:
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
            logger.error(f"Ошибка создания DOCX: {e}\n{traceback.format_exc()}")
            raise

    async def safe_send_document(self, message: Message, path: str):
        try:
            await message.answer_document(FSInputFile(path))
        finally:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except Exception as e:
                    logger.warning(f"Ошибка удаления файла {path}: {e}")

    async def shutdown(self):
        if self.redis:
            await self.redis.close()
        if self.bot:
            await self.bot.session.close()

    async def run(self):
        await self.initialize()
        try:
            await self.dp.start_polling(self.bot)
        except Exception as e:
            logger.critical(f"Критическая ошибка: {str(e)}\n{traceback.format_exc()}")
        finally:
            await self.shutdown()

if __name__ == "__main__":
    app = BotApplication()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.critical(f"Фатальная ошибка: {str(e)}\n{traceback.format_exc()}")