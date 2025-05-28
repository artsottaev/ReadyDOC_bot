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
        self.current_chat_id = None
        
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
            waiting_for_special_terms = State()
            current_variable = State()
        
        self.states = DocGenState
        self.register_handlers()

    def extract_entities(self, text: str) -> dict:
        doc = Doc(text)
        doc.segment(self.segmenter)
        doc.tag_morph(self.morph_tagger)
        doc.parse_syntax(self.syntax_parser)
        doc.tag_ner(self.ner_tagger)
        
        organisations = []
        names = []
        
        for span in doc.spans:
            if span.type == "ORG":
                organisations.append(span.text)
            elif span.type == "PER":
                names.append(span.text)
        
        return {
            'organisations': organisations,
            'names': names
        }

    async def identify_roles(self, document_text: str) -> dict:
        """Используем ИИ для определения ролей участников договора"""
        try:
            response = await self.generate_gpt_response(
                system_prompt="""Ты юридический ассистент. Определи роли участников договора и их реквизиты.
                Ответь в формате JSON:
                {
                    "roles": {
                        "Роль1": ["ТИП_ДАННЫХ_1", "ТИП_ДАННЫХ_2", ...],
                        "Роль2": ["ТИП_ДАННЫХ_1", ...]
                    }
                }
                Пример: 
                {
                    "roles": {
                        "Арендодатель": ["НАЗВАНИЕ_ОРГАНИЗАЦИИ", "ИНН", "АДРЕС"],
                        "Арендатор": ["ФИО", "ПАСПОРТ"]
                    }
                }""",
                user_prompt=f"Документ:\n{document_text}"
            )
            
            # Извлекаем JSON из ответа
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                import json
                return json.loads(json_match.group(0))
            return {"roles": {}}
        except Exception as e:
            logger.error("Ошибка определения ролей: %s", e)
            return {"roles": {}}

    async def map_variable_to_question(self, var_name: str, context: str, role_info: dict) -> str:
        """Улучшенное формирование вопросов с использованием ИИ"""
        # Сначала попробуем определить роль по контексту
        role = None
        for role_name, fields in role_info.get("roles", {}).items():
            if var_name in fields:
                role = role_name
                break
        
        # Основные шаблоны
        if "название_организации" in var_name.lower():
            return f"Введите полное юридическое название организации {f'для {role}' if role else ''}"
        elif "фио" in var_name.lower():
            return f"Введите ФИО {f'для {role}' if role else ''} (полностью, в формате 'Иванов Иван Иванович')"
        elif "телефон" in var_name.lower():
            return f"Введите телефон {f'для {role}' if role else ''} в формате +7XXXXXXXXXX"
        elif "адрес" in var_name.lower():
            return f"Введите юридический адрес {f'для {role}' if role else ''} (с индексом)"
        elif "инн" in var_name.lower():
            return f"Введите ИНН {f'для {role}' if role else ''} (10 или 12 цифр)"
        elif "дата" in var_name.lower():
            return f"Введите дату {f'для {role}' if role else ''} в формате ДД.ММ.ГГГГ"
        
        # Общий случай
        name = var_name.replace("_", " ").lower()
        return f"Введите {name}{f' для {role}' if role else ''}"

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
                        - Контактные данные: [ТЕЛЕФОН_1], [АДРЕС_1]
                        - Другие реквизиты: [ИНН_1], [ПАСПОРТ_1]""",
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

        @self.dp.callback_query(F.data == "dont_know")
        async def handle_dont_know(callback: types.CallbackQuery, state: FSMContext):
            data = await state.get_data()
            current_var = data['variables'][data['current_variable_index']]
            
            # Предлагаем варианты для пропущенных значений
            suggestions = {
                "дата": datetime.datetime.now().strftime("%d.%m.%Y"),
                "телефон": "+79990001122",
                "инн": "1234567890" if "организации" in current_var.lower() else "123456789012",
                "паспорт": "4510 123456 выдан ОВД г. Москвы 01.01.2020"
            }
            
            # Ищем подходящий вариант
            for pattern, value in suggestions.items():
                if pattern in current_var.lower():
                    await callback.message.answer(
                        f"⚠️ Вы можете использовать временное значение:\n"
                        f"<code>{value}</code>\n\n"
                        f"Позже его нужно будет заменить на актуальное!"
                    )
                    return
            
            await callback.message.answer(
                "⚠️ Это обязательное поле. Если информация неизвестна, "
                "введите <code>НЕТ ДАННЫХ</code> и уточните позже"
            )

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
                    error = "❌ Неверный формат ИНН (должно быть 10 или 12 цифр)"
                elif not await self.validate_inn(value):
                    error = "❌ Недействительный ИНН (проверка не прошла)"
            
            elif current_var.startswith('ТЕЛЕФОН'):
                if not re.match(r'^\+7\d{10}$', value):
                    error = "❌ Неверный формат телефона. Пример: +79998887766"
            
            elif current_var.startswith('ДАТА'):
                try:
                    datetime.datetime.strptime(value, '%d.%m.%Y')
                except ValueError:
                    error = "❌ Неверный формат даты. Используйте ДД.ММ.ГГГГ"
            
            elif current_var.startswith('ПАСПОРТ'):
                if not re.match(r'^\d{4} \d{6}$', value):
                    error = "❌ Неверный формат паспорта. Пример: 4510 123456"
            
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
        
        # Используем ИИ для определения ролей и реквизитов
        role_info = await self.identify_roles(document_text)
        logger.info("Определенные роли: %s", role_info)
        
        # Извлекаем все уникальные переменные
        all_vars = list(set(re.findall(r'\[(.*?)\]', document_text)))
        
        # Группируем переменные по ролям
        grouped_vars = {}
        for var in all_vars:
            # Определяем к какой роли относится переменная
            role = "Общие"
            for role_name, fields in role_info.get("roles", {}).items():
                if var in fields:
                    role = role_name
                    break
                    
            if role not in grouped_vars:
                grouped_vars[role] = []
            grouped_vars[role].append(var)
        
        # Создаем плоский список с сохранением порядка групп
        ordered_vars = []
        var_descriptions = {}
        
        # Сначала общие реквизиты
        if "Общие" in grouped_vars:
            for var in grouped_vars["Общие"]:
                ordered_vars.append(var)
                var_descriptions[var] = await self.map_variable_to_question(var, document_text, role_info)
        
        # Затем специфичные для ролей
        for role, vars_list in grouped_vars.items():
            if role == "Общие":
                continue
                
            # Добавляем разделитель
            ordered_vars.append(f"---{role}---")
            var_descriptions[f"---{role}---"] = f"🔹 {role}"
            
            for var in vars_list:
                ordered_vars.append(var)
                var_descriptions[var] = await self.map_variable_to_question(var, document_text, role_info)
        
        # Логирование всех переменных
        logger.info("Упорядоченные переменные: %s", ordered_vars)
        
        await state.update_data(
            variables=ordered_vars,
            var_descriptions=var_descriptions,
            filled_variables={},
            current_variable_index=0
        )
        await self.ask_next_variable(message, state)

    async def ask_next_variable(self, message: Message, state: FSMContext):
        data = await state.get_data()
        variables = data['variables']
        var_descriptions = data['var_descriptions']
        index = data['current_variable_index']
        
        if index >= len(variables):
            await self.finalize_document(message, state)
            return
            
        current_var = variables[index]
        
        # Если это разделитель группы
        if current_var.startswith("---"):
            await message.answer(f"<b>{var_descriptions[current_var]}</b>")
            await state.update_data(current_variable_index=index + 1)
            await self.ask_next_variable(message, state)
            return
            
        description = var_descriptions[current_var]
        
        # Формируем клавиатуру с подсказками
        keyboard_buttons = []
        
        # Добавляем кнопку пропуска
        keyboard_buttons.append(
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_variable")
        )
        
        # Добавляем кнопку "Не знаю"
        keyboard_buttons.append(
            InlineKeyboardButton(text="❓ Не знаю", callback_data="dont_know")
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[keyboard_buttons])
        
        await state.set_state(self.states.current_variable)
        await message.answer(
            f"✍️ {description}:",
            reply_markup=keyboard
        )

    async def finalize_document(self, message: Message, state: FSMContext):
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
            reviewed_doc = await self.auto_review_and_fix(document_text)
            
            # Проверка заполненности
            missing_vars = set(re.findall(r'\[(.*?)\]', reviewed_doc))
            if missing_vars:
                await message.answer("⚠️ Остались незаполненные поля. Дополнительно проверьте документ.")
        
        filename = f"final_{message.from_user.id}.docx"
        path = self.save_docx(reviewed_doc, filename)
        
        await message.answer_document(FSInputFile(path))
        await message.answer("✅ Документ готов! Проверьте его перед использованием.")
        await state.clear()

        if os.path.exists(path):
            os.unlink(path)

    async def auto_review_and_fix(self, document: str) -> str:
        try:
            async with self.show_loading(self.current_chat_id, ChatAction.TYPING):
                reviewed = await self.generate_gpt_response(
                    system_prompt="""Ты юридический редактор. Проверь документ на:
                    1. Незаполненные поля в квадратных скобках
                    2. Противоречивые условия
                    3. Юридические неточности
                    Если все в порядке, верни тот же текст""",
                    user_prompt=f"Проверь документ:\n\n{document}"
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
                    model="gpt-4-turbo",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.2,
                    max_tokens=4000,
                    response_format={"type": "text"}
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