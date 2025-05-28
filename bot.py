import os
import re
import logging
import asyncio
import tempfile
import traceback
import datetime
import difflib
import httpx
from contextlib import asynccontextmanager
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.enums import ParseMode, ChatAction
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
from natasha import (
    NamesExtractor,
    OrgExtractor,  # Изменено с OrganisationExtractor
    MorphVocab,
    Doc
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
        self.current_chat_id = None
        self.morph_vocab = MorphVocab()

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
            waiting_for_special_terms = State()
            current_variable = State()
        
        self.states = DocGenState
        self.register_handlers()

    def extract_entities(self, text: str) -> dict:
        doc = Doc(text)
        doc.segment(self.morph_vocab)
        
        # Исправленный вызов OrgExtractor
        org_extractor = OrgExtractor(self.morph_vocab)
        name_extractor = NamesExtractor(self.morph_vocab)
        
        doc.orgs = org_extractor(doc)
        doc.names = name_extractor(doc)
        
        return {
            'organisations': [org.fact.as_dict for org in doc.orgs],
            'names': [name.fact.as_dict for name in doc.names]
        }

    def is_requisite(self, context: str, entity_type: str) -> bool:
        keywords = {
            'organisations': ['арендодатель', 'арендатор', 'сторона', 'организация'],
            'names': ['директор', 'представитель', 'лицо', 'подпись']
        }
        return any(kw in context.lower() for kw in keywords[entity_type])

    async def validate_inn(self, inn: str):
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"https://service.nalog.ru/inn-proc.do?inn={inn}",
                    timeout=10
                )
                return response.status_code == 200
            except Exception:
                return False

    @asynccontextmanager
    async def show_loading(self, chat_id: int, action: str = ChatAction.TYPING):
        self.current_chat_id = chat_id
        stop_event = asyncio.Event()
        
        async def loading_animation():
            while not stop_event.is_set():
                await self.bot.send_chat_action(chat_id, action)
                await asyncio.sleep(4.9)
        
        loader_task = asyncio.create_task(loading_animation())
        try:
            yield
        finally:
            stop_event.set()
            await loader_task
            self.current_chat_id = None

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
                    document = await self.generate_gpt_response(
                        system_prompt="""Ты опытный юрист. Составь юридически корректный документ. 
                        Обязательно явно указывай:
                        - Названия организаций в формате [НАЗВАНИЕ_ОРГАНИЗАЦИИ_1]
                        - ФИО ответственных лиц: [ФИО_1]
                        - Контактные данные: [ТЕЛЕФОН_1], [АДРЕС_1]""",
                        user_prompt=f"Составь документ по описанию:\n\n{message.text}"
                    )

                filename = f"draft_{message.from_user.id}.docx"
                path = self.save_docx(document, filename)
                
                await state.update_data(document_text=document)
                await message.answer_document(FSInputFile(path))
                await message.answer(
                    "📄 Черновик готов! Теперь заполним обязательные поля.\n"
                    "Хочешь добавить особые условия? Напиши их или 'нет'."
                )
                await state.set_state(self.states.waiting_for_special_terms)
                
            except Exception as e:
                logger.error("Ошибка обработки: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
                await state.clear()

        @self.dp.message(self.states.waiting_for_special_terms)
        async def handle_additions(message: Message, state: FSMContext):
            try:
                data = await state.get_data()
                base_text = data.get("document_text", "")

                if message.text.strip().lower() == "нет":
                    await self.start_variable_filling(message, state)
                    return

                async with self.show_loading(message.chat.id, ChatAction.TYPING):
                    await message.answer("🔧 Вношу изменения...")
                    updated_doc = await self.generate_gpt_response(
                        system_prompt="Ты юридический редактор. Вноси правки, сохраняя стиль.",
                        user_prompt=f"Добавь условия в документ:\n{message.text}\n\nДокумент:\n{base_text}"
                    )

                await state.update_data(document_text=updated_doc)
                await self.start_variable_filling(message, state)
                
            except Exception as e:
                logger.error("Ошибка обработки: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
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
            
            value = message.text
            error = None

            if current_var.startswith('ИНН'):
                if not (value.isdigit() and len(value) in (10, 12)):
                    error = "❌ Неверный формат ИНН"
                elif not await self.validate_inn(value):
                    error = "❌ Недействительный ИНН"
            
            elif current_var.startswith('ТЕЛЕФОН'):
                if not re.match(r'^\+7\d{10}$', value):
                    error = "❌ Формат: +7XXXXXXXXXX"
            
            elif current_var.startswith('ДАТА'):
                try:
                    datetime.datetime.strptime(value, '%d.%m.%Y')
                except ValueError:
                    error = "❌ Формат даты: ДД.ММ.ГГГГ"
            
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
        
        explicit_vars = list(set(re.findall(r'\[(.*?)\]', document_text)))
        entities = self.extract_entities(document_text)
        
        implicit_vars = []
        for i, org in enumerate(entities['organisations'], 1):
            # Проверка контекста для организаций
            if 'name' in org and self.is_requisite(org['name'], 'organisations'):
                implicit_vars.append(f"НАЗВАНИЕ_ОРГАНИЗАЦИИ_{i}")
        
        for i, name in enumerate(entities['names'], 1):
            # Проверка контекста для имен
            if 'first' in name and self.is_requisite(name['first'], 'names'):
                implicit_vars.append(f"ФИО_{i}")
        
        all_vars = list(set(explicit_vars + implicit_vars))
        
        await state.update_data(
            variables=all_vars,
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
            f"✍️ Введите значение для <b>{current_var}</b>:",
            reply_markup=keyboard
        )

    async def finalize_document(self, message: Message, state: FSMContext):
        data = await state.get_data()
        document_text = data['document_text']
        filled_vars = data['filled_variables']
        
        # Замена явных переменных
        for var in data['variables']:
            document_text = re.sub(
                rf'\[{re.escape(var)}\]', 
                filled_vars.get(var, f"[{var}]"), 
                document_text
            )
        
        # Замена сущностей
        entities = self.extract_entities(document_text)
        for i, org in enumerate(entities['organisations'], 1):
            var_name = f"НАЗВАНИЕ_ОРГАНИЗАЦИИ_{i}"
            if var_name in filled_vars and 'name' in org:
                document_text = document_text.replace(
                    org['name'], 
                    filled_vars[var_name]
                )
        
        async with self.show_loading(message.chat.id, ChatAction.UPLOAD_DOCUMENT):
            reviewed_doc = await self.auto_review_and_fix(document_text)
        
        filename = f"final_{message.from_user.id}.docx"
        path = self.save_docx(reviewed_doc, filename)
        
        await message.answer_document(FSInputFile(path))
        await message.answer("✅ Документ готов!")
        await state.clear()

        if os.path.exists(path):
            os.unlink(path)

    async def auto_review_and_fix(self, document: str) -> str:
        try:
            async with self.show_loading(self.current_chat_id, ChatAction.TYPING):
                reviewed = await self.generate_gpt_response(
                    system_prompt="Исправь ошибки и незаполненные поля в документе",
                    user_prompt=f"Документ для проверки:\n\n{document}"
                )
            
            if reviewed != document:
                diff = difflib.unified_diff(
                    document.splitlines(), 
                    reviewed.splitlines(),
                    fromfile='original',
                    tofile='modified'
                )
                logger.info("Изменения:\n%s", '\n'.join(diff))
            
            return reviewed
        except Exception as e:
            logger.error("Ошибка проверки: %s", e)
            return document

    async def generate_gpt_response(self, system_prompt: str, user_prompt: str) -> str:
        try:
            async with self.show_loading(self.current_chat_id, ChatAction.TYPING):
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
            logger.error("Ошибка OpenAI: %s", e)
            return "❌ Ошибка генерации. Попробуйте позже."

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
            logger.error("Ошибка создания DOCX: %s", e)
            raise

    async def shutdown(self):
        try:
            if self.redis:
                await self.redis.close()
            if self.bot:
                await self.bot.session.close()
        except Exception as e:
            logger.error("Ошибка завершения: %s", e)

    async def run(self):
        await self.initialize()
        try:
            await self.dp.start_polling(self.bot)
        except Exception as e:
            logger.critical("Критическая ошибка: %s", e)
        finally:
            await self.shutdown()

if __name__ == "__main__":
    app = BotApplication()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.critical("Фатальная ошибка: %s", e)