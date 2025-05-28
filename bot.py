import os
import re
import logging
import asyncio
import tempfile
import traceback
import datetime
import difflib
import httpx
import json
from contextlib import asynccontextmanager, nullcontext
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.enums import ParseMode, ChatAction
from aiogram.types import (
    Message, 
    FSInputFile, 
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from openai import AsyncOpenAI
from dotenv import load_dotenv
from docx import Document
from redis.asyncio import Redis
from natasha import (
    Doc,
    Segmenter,
    NewsEmbedding,
    NewsMorphTagger,
    NewsSyntaxParser,
    NewsNERTagger
)

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
        
        # Инициализация компонентов Natasha
        self.segmenter = Segmenter()
        self.emb = NewsEmbedding()
        self.morph_tagger = NewsMorphTagger(self.emb)
        self.syntax_parser = NewsSyntaxParser(self.emb)
        self.ner_tagger = NewsNERTagger(self.emb)

    async def initialize(self):
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("ALL_PROXY", None)

        BOT_TOKEN = os.getenv("BOT_TOKEN")
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        REDIS_URL = os.getenv("REDIS_URL")

        if not all([BOT_TOKEN, OPENAI_API_KEY, REDIS_URL]):
            raise EnvironmentError(
                "Не заданы обязательные переменные окружения: "
                "BOT_TOKEN, OPENAI_API_KEY, REDIS_URL"
            )

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

        self.redis = Redis.from_url(
            REDIS_URL,
            socket_timeout=10,
            socket_connect_timeout=5,
            retry_on_timeout=True,
            decode_responses=True,
            health_check_interval=30
        )
        
        if not await self.redis.ping():
            raise ConnectionError("Не удалось подключиться к Redis")

        storage = RedisStorage(redis=self.redis)
        self.bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
        self.dp = Dispatcher(storage=storage)

        class DocGenState(StatesGroup):
            waiting_for_initial_input = State()
            current_variable = State()
            document_review = State()
            waiting_for_special_terms = State()  # Перенесли в конец
        
        self.states = DocGenState
        self.register_handlers()

    # ... (остальные методы без изменений до register_handlers) ...

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
                logger.error("Ошибка в /start: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Произошла внутренняя ошибка. Попробуйте позже.")

        @self.dp.message(self.states.waiting_for_initial_input)
        async def handle_description(message: Message, state: FSMContext):
            try:
                if len(message.text) > 3000:
                    await message.answer("⚠️ Слишком длинный текст. Укороти, пожалуйста.")
                    return

                await state.update_data(initial_text=message.text)
                
                async with self.show_loading(message.chat.id, ChatAction.UPLOAD_DOCUMENT):
                    await message.answer("🧠 Генерирую черновик документа...")
                    
                    # Улучшенный промпт для точного определения ролей
                    document = await self.generate_gpt_response(
                        system_prompt="""Ты опытный юрист. Составь юридически корректный документ. 
                        Обязательно:
                        1. Не делай предположений о ролях сторон (арендодатель/арендатор) без явных указаний
                        2. Если роли не указаны явно, используй нейтральные названия: Сторона 1, Сторона 2
                        3. Явно указывай:
                           - Названия организаций в формате [НАЗВАНИЕ_ОРГАНИЗАЦИИ]
                           - ФИО ответственных лиц: [ФИО]
                           - Контактные данные: [ТЕЛЕФОН], [АДРЕС]
                           - Другие реквизиты: [ИНН], [ОГРН], [ПАСПОРТ] для ИП, [ДОЛЖНОСТЬ] для ООО
                           - Суммы и сроки: [СУММА], [СРОК]""",
                        user_prompt=f"Составь документ по описанию:\n\n{message.text}",
                        chat_id=message.chat.id
                    )

                filename = f"draft_{message.from_user.id}.docx"
                path = self.save_docx(document, filename)
                
                await state.update_data(document_text=document)
                await message.answer_document(FSInputFile(path))
                await message.answer(
                    "📄 Черновик готов! Теперь заполним обязательные реквизиты."
                )
                # Сразу переходим к заполнению реквизитов
                await self.start_variable_filling(message, state)
                
            except Exception as e:
                logger.error("Ошибка обработки: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
                await state.clear()

        # Убираем старый обработчик для waiting_for_special_terms

        @self.dp.callback_query(F.data == "skip_variable")
        async def handle_skip_variable(callback: types.CallbackQuery, state: FSMContext):
            data = await state.get_data()
            index = data['current_variable_index'] + 1
            await state.update_data(current_variable_index=index)
            await callback.message.delete()
            await self.ask_next_variable(callback.message, state)

        @self.dp.callback_query(F.data == "dont_know")
        async def handle_dont_know(callback: types.CallbackQuery, state: FSMContext):
            # ... (без изменений) ...

        @self.dp.message(self.states.current_variable)
        async def handle_variable_input(message: Message, state: FSMContext):
            # ... (без изменений) ...

        @self.dp.callback_query(F.data == "confirm_document")
        async def handle_confirm_document(callback: types.CallbackQuery, state: FSMContext):
            await callback.message.delete()
            await self.send_final_document(callback.message, state)

        @self.dp.callback_query(F.data == "edit_document")
        async def handle_edit_document(callback: types.CallbackQuery, state: FSMContext):
            await callback.message.delete()
            await state.set_state(self.states.waiting_for_initial_input)
            await callback.message.answer("🔄 Введите новый запрос для генерации документа:")

        # Новый обработчик для добавления условий в конце
        @self.dp.callback_query(F.data == "add_terms")
        async def handle_add_terms(callback: types.CallbackQuery, state: FSMContext):
            await callback.message.answer(
                "✍️ Хотите добавить особые условия? Напишите их или 'нет':"
            )
            await state.set_state(self.states.waiting_for_special_terms)

        @self.dp.message(self.states.waiting_for_special_terms)
        async def handle_final_additions(message: Message, state: FSMContext):
            try:
                data = await state.get_data()
                
                if message.text.strip().lower() == "нет":
                    await self.send_final_document(message, state)
                    return
                
                base_text = data.get("final_document", "")
                
                async with self.show_loading(message.chat.id, ChatAction.TYPING):
                    await message.answer("🔧 Вношу изменения в документ...")
                    updated_doc = await self.generate_gpt_response(
                        system_prompt="Ты юридический редактор. Внеси правки, добавив особые условия. Сохрани структуру документа.",
                        user_prompt=f"Добавь условия в документ:\n{message.text}\n\nДокумент:\n{base_text}",
                        chat_id=message.chat.id
                    )

                filename = f"final_{message.from_user.id}.docx"
                path = self.save_docx(updated_doc, filename)
                
                await message.answer_document(FSInputFile(path))
                await message.answer("✅ Документ обновлен!")
                await state.clear()
                
                if os.path.exists(path):
                    os.unlink(path)
                    
            except Exception as e:
                logger.error("Ошибка обработки: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
                await state.clear()

    # ... (остальные методы без изменений) ...

    async def prepare_final_document(self, message: Message, state: FSMContext):
        data = await state.get_data()
        document_text = data['document_text']
        filled_vars = data['filled_variables']
        
        # Замена переменных
        for var in data['variables']:
            if var.startswith("---"):
                continue
                
            if var in filled_vars:
                document_text = re.sub(
                    rf'\[{re.escape(var)}\]', 
                    filled_vars[var], 
                    document_text
                )
        
        async with self.show_loading(message.chat.id, ChatAction.UPLOAD_DOCUMENT):
            # Автоматическая проверка и фикс
            reviewed_doc = await self.auto_review_and_fix(document_text, message.chat.id)
            
            # Проверка заполненности
            missing_vars = set(re.findall(r'\[(.*?)\]', reviewed_doc))
            if missing_vars:
                await message.answer(
                    f"⚠️ В документе остались незаполненные поля: {', '.join(missing_vars)}\n"
                    "Пожалуйста, проверьте документ перед отправкой."
                )
            
            # Сохраняем результат для финального подтверждения
            filename = f"prefinal_{message.from_user.id}.docx"
            path = self.save_docx(reviewed_doc, filename)
            
            await state.update_data(
                final_document=reviewed_doc,
                document_path=path
            )
            
            # Отправляем документ на подтверждение
            await message.answer_document(FSInputFile(path))
            
            # Новая клавиатура с вопросом про особые условия
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Завершить", callback_data="confirm_document"),
                    InlineKeyboardButton(text="✏️ Добавить условия", callback_data="add_terms")
                ],
                [
                    InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="edit_document")
                ]
            ])
            
            await message.answer(
                "📝 Документ готов! Вы можете:\n"
                "- Завершить и получить финальную версию\n"
                "- Добавить особые условия\n"
                "- Перегенерировать документ с нуля",
                reply_markup=keyboard
            )
            await state.set_state(self.states.document_review)

    async def send_final_document(self, message: Message, state: FSMContext):
        data = await state.get_data()
        document_text = data.get('final_document', '')
        path = data.get('document_path', '')
        
        if not document_text:
            await message.answer("⚠️ Ошибка: документ не найден")
            await state.clear()
            return
        
        # Сохраняем финальную версию
        filename = f"final_{message.from_user.id}.docx"
        final_path = self.save_docx(document_text, filename)
        
        await message.answer_document(FSInputFile(final_path))
        await message.answer("✅ Документ готов! Проверьте его перед использованием.")
        await state.clear()

        # Удаляем временные файлы
        for file_path in [path, final_path]:
            if file_path and os.path.exists(file_path):
                os.unlink(file_path)

    # ... (остальные методы без изменений) ...

if __name__ == "__main__":
    app = BotApplication()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.critical("Фатальная ошибка: %s", e)