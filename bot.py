import os
import re
import logging
import asyncio
import tempfile
import traceback
import datetime
import httpx
import json
from contextlib import asynccontextmanager
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.enums import ParseMode, ChatAction
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from openai import AsyncOpenAI
from dotenv import load_dotenv
from docx import Document
from redis.asyncio import Redis
from natasha import Doc, Segmenter, NewsEmbedding, NewsNERTagger

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
            waiting_for_special_terms = State()
        
        self.states = DocGenState
        self.register_handlers()

    def extract_entities(self, text: str) -> dict:
        doc = Doc(text)
        doc.segment(self.segmenter)
        doc.tag_ner(self.ner_tagger)
        
        organisations = []
        for span in doc.spans:
            if span.type == "ORG":
                org_name = span.normal if span.normal else span.text
                organisations.append(org_name)
        
        return {'organisations': organisations}

    async def identify_roles(self, document_text: str) -> dict:
        try:
            response = await self.generate_gpt_response(
                system_prompt="""Ты юридический ассистент. Определи роли участников договора и их реквизиты.
                Ответь в формате JSON:
                {
                    "roles": {
                        "Роль1": {
                            "fields": ["ТИП_ЛИЦА", "ПАСПОРТНЫЕ_ДАННЫЕ_ИЛИ_РЕКВИЗИТЫ", "ИНН", "ОГРНИП_ИЛИ_ОГРН", "БАНКОВСКИЕ_РЕКВИЗИТЫ"]
                        },
                        "Роль2": {
                            "fields": ["ТИП_ЛИЦА", "ПАСПОРТНЫЕ_ДАННЫЕ_ИЛИ_РЕКВИЗИТЫ", "ИНН", "ОГРНИП_ИЛИ_ОГРН"]
                        }
                    },
                    "field_descriptions": {
                        "ТИП_ЛИЦА": "Тип лица (физическое лицо, ИП, ООО)",
                        "ПЛОЩАДЬ": "Площадь помещения в кв.м.",
                        "КАДАСТРОВЫЙ_НОМЕР": "Кадастровый номер помещения",
                        "НДС": "Включен ли НДС в арендную плату (да/нет)"
                    }
                }""",
                user_prompt=f"Документ:\n{document_text}",
                chat_id=None
            )
            
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(0))
            return {"roles": {}, "field_descriptions": {}}
        except Exception as e:
            logger.error("Ошибка определения ролей: %s", e)
            return {"roles": {}, "field_descriptions": {}}

    def map_variable_to_question(self, var_name: str, role_info: dict) -> str:
        role = None
        for role_name, role_data in role_info.get("roles", {}).items():
            if var_name in role_data.get("fields", []):
                role = role_name
                break
        
        description = role_info.get("field_descriptions", {}).get(var_name, var_name.replace("_", " ").lower())
        
        if role:
            return f"✍️ Введите <b>{description}</b> для <b>{role}</b>:"
        return f"✍️ Введите <b>{description}</b>:"

    def validate_inn(self, inn: str) -> bool:
        return inn.isdigit() and len(inn) in (10, 12)
    
    def num2words(self, num: int) -> str:
        """Конвертирует число в прописной формат (упрощенная версия)"""
        units = ['', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять']
        teens = ['десять', 'одиннадцать', 'двенадцать', 'тринадцать', 'четырнадцать', 'пятнадцать', 
                'шестнадцать', 'семнадцать', 'восемнадцать', 'девятнадцать']
        tens = ['', '', 'двадцать', 'тридцать', 'сорок', 'пятьдесят', 
               'шестьдесят', 'семьдесят', 'восемьдесят', 'девяносто']
        hundreds = ['', 'сто', 'двести', 'триста', 'четыреста', 'пятьсот', 
                   'шестьсот', 'семьсот', 'восемьсот', 'девятьсот']
        
        def _convert(n):
            if n < 10:
                return units[n]
            elif 10 <= n < 20:
                return teens[n-10]
            elif 20 <= n < 100:
                return tens[n//10] + (' ' + units[n%10] if n%10 !=0 else '')
            elif 100 <= n < 1000:
                return hundreds[n//100] + (' ' + _convert(n%100) if n%100 !=0 else '')
            return ''
        
        return _convert(num).strip()

    @asynccontextmanager
    async def show_loading(self, chat_id: int, action: str = ChatAction.TYPING):
        if chat_id is None:
            yield
            return
            
        stop_event = asyncio.Event()
        
        async def loading_animation():
            while not stop_event.is_set():
                try:
                    await self.bot.send_chat_action(chat_id, action)
                except Exception as e:
                    logger.error("Ошибка отправки действия: %s", e)
                await asyncio.sleep(4.9)
        
        loader_task = asyncio.create_task(loading_animation())
        try:
            yield
        finally:
            stop_event.set()
            try:
                await loader_task
            except:
                pass

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
                        КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:
                        1. Для физических лиц: указать "действующий от своего имени" и паспортные данные
                        2. Для ИП: указать "ИП [ФИО], действующий на основании свидетельства ОГРНИП"
                        3. Для ООО: указать "в лице [ДОЛЖНОСТЬ] [ФИО], действующего на основании устава"
                        4. В предмете договора обязательно указать:
                           - Точный адрес с номером помещения
                           - Площадь помещения
                           - Кадастровый номер
                        5. В арендной плате:
                           - Указать валюту (рубли)
                           - Уточнить включен ли НДС
                           - Прописать сумму прописью
                        6. Добавить разделы:
                           - Коммунальные платежи
                           - Порядок расторжения
                           - Реквизиты сторон
                        7. В подписях указать:
                           - Для ИП: ИНН и ОГРНИП
                           - Для физлиц: паспортные данные
                           - Для ООО: ИНН, ОГРН, КПП
                        8. Проверить согласованность дат""",
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
                await self.start_variable_filling(message, state)
                
            except Exception as e:
                logger.error("Ошибка обработки: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
                await state.clear()

        # ... остальные обработчики (skip_variable, dont_know) остаются без изменений ...

        @self.dp.message(self.states.current_variable)
        async def handle_variable_input(message: Message, state: FSMContext):
            data = await state.get_data()
            variables = data['variables']
            index = data['current_variable_index']
            current_var = variables[index]
            role_info = data.get('role_info', {})
            
            current_role = "документа"
            for role_name, role_data in role_info.get("roles", {}).items():
                if current_var in role_data.get("fields", []):
                    current_role = role_name
                    break

            value = message.text
            error = None

            if "инн" in current_var.lower():
                if not self.validate_inn(value):
                    error = (
                        "❌ Неверный формат ИНН\n"
                        f"Этот ИНН нужен для: <b>{current_role}</b>\n\n"
                        "Формат:\n- 10 цифр для организаций\n- 12 цифр для ИП/физлиц\n"
                        "Пример: <code>1234567890</code> или <code>123456789012</code>"
                    )
            
            elif "телефон" in current_var.lower():
                if not re.match(r'^\+7\d{10}$', value):
                    error = (
                        "❌ Неверный формат телефона\n"
                        f"Этот телефон нужен для: <b>{current_role}</b>\n\n"
                        "Формат: +7 и 10 цифр без пробелов\n"
                        "Пример: <code>+79998887766</code>"
                    )
            
            elif "дата" in current_var.lower():
                try:
                    datetime.datetime.strptime(value, '%d.%m.%Y')
                except ValueError:
                    error = (
                        "❌ Неверный формат даты\n"
                        f"Эта дата нужна для: <b>{current_role}</b>\n\n"
                        "Используйте формат: ДД.ММ.ГГГГ\n"
                        "Пример: <code>01.01.2023</code>"
                    )
            
            elif "паспорт" in current_var.lower():
                if not re.match(r'^\d{4} \d{6}$', value):
                    error = (
                        "❌ Неверный формат паспорта\n"
                        f"Эти данные нужны для: <b>{current_role}</b>\n\n"
                        "Формат: серия (4 цифры) и номер (6 цифр) через пробел\n"
                        "Пример: <code>4510 123456</code>"
                    )
            
            elif "сумма" in current_var.lower():
                if not re.match(r'^[\d\s]+$', value):
                    error = (
                        "❌ Неверный формат суммы\n"
                        f"Эта сумма нужна для: <b>{current_role}</b>\n\n"
                        "Используйте цифры (можно с пробелами)\n"
                        "Примеры: <code>10000</code> или <code>15 000</code>"
                    )
            
            elif "огрн" in current_var.lower():
                if len(value) not in [13, 15] or not value.isdigit():
                    error = (
                        "❌ Неверный формат ОГРН/ОГРНИП\n"
                        f"Этот реквизит нужен для: <b>{current_role}</b>\n\n"
                        "Формат:\n- 13 цифр для ОГРН\n- 15 цифр для ОГРНИП\n"
                        "Пример: <code>1234567890123</code>"
                    )
            
            elif "тип_лица" in current_var.lower():
                if value.lower() not in ["физическое лицо", "ип", "ооо"]:
                    error = "❌ Укажите корректный тип лица: Физическое лицо, ИП или ООО"
            
            elif "площадь" in current_var.lower():
                if not re.match(r'^\d+(\.\d+)?$', value):
                    error = "❌ Площадь должна быть числом (разделитель - точка)"
            
            elif "кадастровый_номер" in current_var.lower():
                if not re.match(r'^\d{2}:\d{2}:\d{6,7}:\d+$', value):
                    error = "❌ Неверный формат кадастрового номера. Пример: 77:01:0001010:123"
            
            elif "ндс" in current_var.lower():
                if value.lower() not in ["да", "нет"]:
                    error = "❌ Укажите: 'да' если НДС включен, 'нет' если не включен"
            
            elif "банковск" in current_var.lower():
                if len(re.findall(r'\d', value)) < 20:
                    error = "❌ Укажите полные банковские реквизиты (БИК и расчетный счет)"

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

        # ... остальные обработчики (confirm_document, edit_document и т.д.) ...

    async def start_variable_filling(self, message: Message, state: FSMContext):
        data = await state.get_data()
        document_text = data['document_text']
        role_info = await self.identify_roles(document_text)
        
        all_vars = list(set(re.findall(r'\[(.*?)\]', document_text)))
        
        # Добавляем обязательные поля, если их нет
        required_vars = ["ТИП_ЛИЦА_АРЕНДОДАТЕЛЯ", "ТИП_ЛИЦА_АРЕНДАТОРА", "ПЛОЩАДЬ", "КАДАСТРОВЫЙ_НОМЕР", "НДС"]
        for var in required_vars:
            if var not in all_vars:
                all_vars.append(var)
                role_info["field_descriptions"][var] = var.replace("_", " ").lower()
                if "Арендодатель" in role_info.get("roles", {}):
                    role_info["roles"]["Арендодатель"]["fields"].append(var)
        
        # ... остальная логика группировки переменных ...

    async def prepare_final_document(self, message: Message, state: FSMContext):
        data = await state.get_data()
        document_text = data['document_text']
        filled_vars = data['filled_variables']
        
        # Замена переменных
        for var in data['variables']:
            if var.startswith("---"):
                continue
                
            if var in filled_vars:
                value = filled_vars[var]
                
                # Для сумм добавляем прописную форму
                if "сумма" in var.lower() and value.replace(" ", "").isdigit():
                    num = int(value.replace(" ", ""))
                    value = f"{value} ({self.num2words(num)} рублей)"
                
                document_text = re.sub(
                    rf'\[{re.escape(var)}\]', 
                    value, 
                    document_text
                )
        
        # Добавляем блоки с реквизитами
        if "Арендодатель" in document_text:
            document_text += (
                "\n\n**Арендодатель:**\n"
                f"{filled_vars.get('НАЗВАНИЕ_ОРГАНИЗАЦИИ_АРЕНДОДАТЕЛЯ', '')}\n"
                f"ИНН: {filled_vars.get('ИНН_АРЕНДОДАТЕЛЯ', '')}\n"
                f"ОГРН/ОГРНИП: {filled_vars.get('ОГРНИП_ИЛИ_ОГРН_АРЕНДОДАТЕЛЯ', '')}\n"
                "______________________   / [Подпись] /"
            )
            
        if "Арендатор" in document_text:
            document_text += (
                "\n\n**Арендатор:**\n"
                f"{filled_vars.get('НАЗВАНИЕ_ОРГАНИЗАЦИИ_АРЕНДАТОРА', '')}\n"
                f"ИНН: {filled_vars.get('ИНН_АРЕНДАТОРА', '')}\n"
                f"ОГРН/ОГРНИП: {filled_vars.get('ОГРНИП_ИЛИ_ОГРН_АРЕНДАТОРА', '')}\n"
                "______________________   / [Подпись] /"
            )
        
        # ... остальная логика ...

    async def auto_review_and_fix(self, document: str, chat_id: int) -> str:
        try:
            async with self.show_loading(chat_id, ChatAction.TYPING):
                reviewed = await self.generate_gpt_response(
                    system_prompt="""Ты юридический редактор. Проверь документ и ВНЕСИ ИСПРАВЛЕНИЯ:
                    1. Проверь согласованность дат (дата договора должна быть позже даты начала аренды)
                    2. Убедись что для физлиц не указаны реквизиты юрлиц
                    3. Проверь что для ИП не указаны данные гендиректора
                    4. Проверь наличие всех существенных условий договора
                    5. Добавь сумму прописью если она указана только цифрами
                    6. Убедись что указаны:
                       - Кадастровый номер
                       - Площадь помещения
                       - Реквизиты сторон
                    7. Удали все примерные значения""",
                    user_prompt=f"Исправь этот документ:\n\n{document}",
                    chat_id=chat_id
                )
            
            if "```" in reviewed:
                reviewed = reviewed.split("```")[1]
            return reviewed.strip()
        
        except Exception as e:
            logger.error("Ошибка проверки: %s", e)
            return document

    # ... остальные методы без изменений ...

if __name__ == "__main__":
    app = BotApplication()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.critical("Фатальная ошибка: %s", e)