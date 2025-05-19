import os
import re
import logging
import asyncio
import tempfile
import traceback
import datetime
import difflib
import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.enums import ParseMode
from aiogram.types import (
    Message, 
    FSInputFile, 
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
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
            current_variable = State()
        
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
                    system_prompt="Ты опытный юрист. Составь юридически корректный документ. "
                                  "Важные требования:\n"
                                  "- Все изменения должны быть обратимы через переменные\n"
                                  "- Избегай ситуаций, требующих последующей проверки\n"
                                  "- Явно маркируй спорные моменты как [КОММЕНТАРИЙ: ...]",
                    user_prompt=f"Составь юридический документ по российскому праву. Вот описание от пользователя:\n\n\"{message.text}\""
                )

                filename = f"draft_{message.from_user.id}.docx"
                path = self.save_docx(document, filename)
                
                await state.update_data(document_text=document)
                await message.answer_document(FSInputFile(path))
                await message.answer(
                    "📄 Черновик документа готов! Теперь нужно заполнить обязательные поля.\n"
                    "Хочешь добавить особые условия? Напиши их или напиши <b>нет</b>."
                )
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
                    await self.start_variable_filling(message, state)
                    return

                await message.answer("🔧 Вношу изменения...")
                updated_doc = await self.generate_gpt_response(
                    system_prompt="Ты юридический редактор. Вноси только необходимые правки, сохраняя стиль.",
                    user_prompt=(
                        "Вот документ. Добавь в него аккуратно следующие особые условия, "
                        f"сохранив стиль и структуру:\n\nУсловия: {message.text}\n\nДокумент:\n{base_text}"
                    )
                )

                await state.update_data(document_text=updated_doc)
                await self.start_variable_filling(message, state)
                
            except Exception as e:
                logger.error(f"Ошибка обработки условий: {e}\n{traceback.format_exc()}")
                await message.answer("⚠️ Произошла ошибка при обработке условий. Попробуйте снова.")
                await state.clear()

        @self.dp.callback_query(F.data == "skip_variable")
        async def handle_skip_variable(callback: types.CallbackQuery, state: FSMContext):
            data = await state.get_data()
            index = data['current_variable_index'] + 1
            await state.update_data(current_variable_index=index)
            await callback.message.delete()
            await self.ask_next_variable(callback.message, state)

        @self.dp.message(self.states.current_variable)
        async def handle_variable_input(message: Message, state: FSMContext):
            data = await state.get_data()
            variables = data['variables']
            index = data['current_variable_index']
            current_var = variables[index]
            
            # Валидация ввода
            error = None
            value = message.text
            
            if current_var.upper() == "ИНН":
                if not value.isdigit() or len(value) not in [10, 12]:
                    error = "❌ Неверный формат ИНН! Должно быть 10 или 12 цифр"
            elif "ДАТА" in current_var.upper():
                try:
                    datetime.datetime.strptime(value, '%d.%m.%Y')
                except ValueError:
                    error = "❌ Неверный формат даты! Используйте ДД.ММ.ГГГГ"
            elif "СУММА" in current_var.upper():
                if not value.replace(' ', '').replace(',', '.').replace('.', '', 1).isdigit():
                    error = "❌ Неверный формат суммы! Пример: 15000 или 12 345,67"
            
            if error:
                await message.answer(error)
                return

            filled = data['filled_variables']
            filled[current_var] = value
            
            await state.update_data(
                filled_variables=filled,
                current_variable_index=index + 1
            )
            
            await self.ask_next_variable(message, state)

    async def start_variable_filling(self, message: Message, state: FSMContext):
        data = await state.get_data()
        document_text = data['document_text']
        
        variables = list(set(re.findall(r'\[(.*?)\]', document_text)))
        
        await state.update_data(
            variables=variables,
            filled_variables={},
            current_variable_index=0
        )
        
        await self.ask_next_variable(message, state)

    async def ask_next_variable(self, message: Message, state: FSMContext):
        data = await state.get_data()
        variables = data['variables']
        index = data['current_variable_index']
        
        if index >= len(variables):
            await self.finalize_document(message, state)
            return
            
        current_var = variables[index]
        await state.set_state(self.states.current_variable)
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_variable")
        ]])
        
        await message.answer(
            f"✍️ Введите значение для переменной <b>{current_var}</b>:",
            reply_markup=keyboard
        )

    async def finalize_document(self, message: Message, state: FSMContext):
        data = await state.get_data()
        document_text = data['document_text']
        filled_vars = data['filled_variables']
        
        # Замена переменных
        for var in data['variables']:
            document_text = re.sub(
                rf'\[{re.escape(var)}\]', 
                filled_vars.get(var, f"[{var}]"), 
                document_text
            )
        
        # Автоматическая проверка и коррекция
        reviewed_doc = await self.auto_review_and_fix(document_text)
        
        # Сохраняем и отправляем
        filename = f"final_{message.from_user.id}.docx"
        path = self.save_docx(reviewed_doc, filename)
        
        await message.answer_document(FSInputFile(path))
        await message.answer("✅ Документ проверен и готов к использованию!")
        await state.clear()

        if os.path.exists(path):
            os.unlink(path)

    async def auto_review_and_fix(self, document: str) -> str:
        try:
            reviewed = await self.generate_gpt_response(
                system_prompt="""Ты опытный юридический редактор. Автоматически исправь:
1. Незаполненные переменные [ВОТ_ТАК]
2. Логические противоречия
3. Ошибки в нумерации
4. Недочеты в тексте документа
5. Несоответствие российскому законодательству на 2025 год

Формат правок:
- ТОЛЬКО исправления без комментариев
- Сохрани исходную структуру
- Не упоминай о внесенных изменениях""",
                
                user_prompt=f"Проверь, проанализируй и молча исправь документ:\n\n{document}"
            )
            
            # Логирование изменений
            if reviewed != document:
                diff = difflib.unified_diff(
                    document.splitlines(), 
                    reviewed.splitlines(),
                    fromfile='original',
                    tofile='modified'
                )
                logger.info(f"Auto-correct diff:\n" + "\n".join(diff))
            
            return reviewed
            
        except Exception as e:
            logger.error(f"Ошибка авто-проверки: {e}\n{traceback.format_exc()}")
            return document  # Возвращаем оригинал при ошибке

    async def generate_gpt_response(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = await self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo-0125",
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

    async def shutdown(self):
        try:
            if self.redis:
                await self.redis.close()
            if self.bot:
                await self.bot.session.close()
        except Exception as e:
            logger.error(f"Ошибка при завершении работы: {e}")

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