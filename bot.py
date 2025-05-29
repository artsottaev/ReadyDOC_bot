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
            waiting_for_rent_details = State()  # Детали аренды
            waiting_for_parties_info = State()
            current_variable = State()
            document_review = State()
            waiting_for_special_terms = State()
            waiting_for_additional_clauses = State()  # Дополнительные условия
        
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
                        "Арендодатель": {
                            "fields": ["ТИП_ЛИЦА", "ПАСПОРТНЫЕ_ДАННЫЕ", "ИНН", "ОГРНИП_ИЛИ_ОГРН", "БАНКОВСКИЕ_РЕКВИЗИТЫ"]
                        },
                        "Арендатор": {
                            "fields": ["ТИП_ЛИЦА", "ПАСПОРТНЫЕ_ДАННЫЕ", "ИНН", "ОГРНИП_ИЛИ_ОГРН", "БАНКОВСКИЕ_РЕКВИЗИТЫ"]
                        }
                    },
                    "field_descriptions": {
                        "ТИП_ЛИЦА": "Тип лица (физическое лицо, ИП, ООО)",
                        "ПЛОЩАДЬ": "Площадь помещения в кв.м.",
                        "КАДАСТРОВЫЙ_НОМЕР": "Кадастровый номер помещения",
                        "АРЕНДНАЯ_ПЛАТА": "Сумма арендной платы",
                        "СРОК_АРЕНДЫ": "Срок действия договора",
                        "ДАТА_НАЧАЛА": "Дата начала аренды",
                        "ДАТА_ОКОНЧАНИЯ": "Дата окончания аренды",
                        "СТАВКА_НДС": "Ставка НДС (%)",
                        "КОММУНАЛЬНЫЕ_ПЛАТЕЖИ": "Кто оплачивает коммунальные платежи",
                        "ДЕПОЗИТ": "Сумма депозита"
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
                    "🏢 <b>Юридический помощник по аренде коммерческой недвижимости</b>\n\n"
                    "Я помогу составить договор аренды для вашего бизнеса:\n"
                    "- Кофейни, магазина, салона красоты\n"
                    "- Офиса или коворкинга\n"
                    "- Производственного помещения\n\n"
                    "Просто опишите, какой договор вам нужен. Например:\n"
                    "<i>Нужен договор аренды офиса 30 м² в Москве на 1 год</i>"
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
                
                # Проверяем, относится ли запрос к аренде
                is_rental = any(keyword in message.text.lower() for keyword in 
                               ["аренд", "съем", "помещен", "площад", "офис", "магазин", "кафе", "салон"])
                
                await state.update_data(is_rental=is_rental)
                
                # Определяем тип бизнеса
                business_type = "other"
                text_lower = message.text.lower()
                if any(kw in text_lower for kw in ["кафе", "кофейн", "ресторан", "столов", "бар"]):
                    business_type = "cafe"
                elif any(kw in text_lower for kw in ["магазин", "торгов", "рознич", "бутик"]):
                    business_type = "shop"
                elif any(kw in text_lower for kw in ["салон красот", "парикмахер", "ногтев", "косметолог"]):
                    business_type = "beauty"
                
                await state.update_data(business_type=business_type)
                
                if is_rental:
                    await message.answer(
                        "🏢 <b>Уточните детали аренды:</b>\n\n"
                        "1. <b>Тип помещения:</b>\n"
                        "   - Офисное\n   - Торговое\n   - Производственное\n   - Складское\n"
                        "2. <b>Площадь:</b> (в кв.м.)\n"
                        "3. <b>Мебель/техника:</b> (да/нет)\n"
                        "4. <b>Система налогообложения арендодателя:</b> (ОСН/УСН/Патент)\n"
                        "5. <b>Особые условия:</b> (депозит, коммунальные платежи, субаренда)\n\n"
                        "<i>Пример: Офисное помещение 35 м², без мебели, арендодатель на УСН, "
                        "коммунальные платежи включены в аренду, депозит 2 месяца</i>"
                    )
                    await state.set_state(self.states.waiting_for_rent_details)
                else:
                    await message.answer(
                        "👥 <b>Укажите стороны договора:</b>\n\n"
                        "<i>Пример:\nАрендодатель: ООО 'Ромашка'\n"
                        "Арендатор: Иван Иванов (ИП)</i>"
                    )
                    await state.set_state(self.states.waiting_for_parties_info)
                
            except Exception as e:
                logger.error("Ошибка обработки: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
                await state.clear()

        @self.dp.message(self.states.waiting_for_rent_details)
        async def handle_rent_details(message: Message, state: FSMContext):
            try:
                rent_details = message.text
                await state.update_data(rent_details=rent_details)
                
                # Извлекаем структурированные параметры аренды
                async with self.show_loading(message.chat.id, ChatAction.TYPING):
                    rental_params = await self.extract_rental_params(rent_details)
                    await state.update_data(rental_params=rental_params)
                    
                    # Проверяем обязательные параметры
                    if not rental_params.get("property_type") or not rental_params.get("area"):
                        await message.answer(
                            "⚠️ <b>Не указаны ключевые параметры:</b>\n"
                            "Пожалуйста, укажите как минимум:\n"
                            "- Тип помещения\n"
                            "- Площадь\n\n"
                            "<i>Пример: Офис 50 м², арендодатель на УСН</i>"
                        )
                        return
                    
                    # Формируем рекомендации по налогам
                    tax_system = rental_params.get("tax_system", "").upper()
                    tax_advice = ""
                    if tax_system == "УСН":
                        tax_advice = "✅ Арендодатель на УСН: НДС не начисляется"
                    elif tax_system == "ОСН":
                        tax_advice = "⚠️ Арендодатель на ОСН: Включите НДС в стоимость"
                    else:
                        tax_advice = "ℹ️ Уточните систему налогообложения арендодателя"
                    
                    await message.answer(
                        f"📋 <b>Параметры аренды:</b>\n"
                        f"Тип: {rental_params.get('property_type', 'не указан')}\n"
                        f"Площадь: {rental_params.get('area', 'не указана')} м²\n"
                        f"Мебель: {rental_params.get('furnished', 'не указано')}\n"
                        f"Налогообложение: {tax_system}\n\n"
                        f"{tax_advice}"
                    )
                    
                    # Переходим к сбору информации о сторонах
                    await message.answer(
                        "👥 <b>Теперь укажите стороны договора:</b>\n\n"
                        "<b>Арендодатель:</b>\n"
                        "- ФИО/Название организации\n"
                        "- Тип лица (физлицо, ИП, ООО)\n"
                        "\n<b>Арендатор:</b>\n"
                        "- ФИО/Название организации\n"
                        "- Тип лица\n\n"
                        "<i>Пример:\nАрендодатель: ИП Сидоров А.В.\n"
                        "Арендатор: ООО 'Вектор'</i>"
                    )
                    await state.set_state(self.states.waiting_for_parties_info)
                
            except Exception as e:
                logger.error("Ошибка обработки деталей аренды: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки деталей аренды. Попробуйте снова.")
                await state.clear()

        @self.dp.message(self.states.waiting_for_parties_info)
        async def handle_parties_info(message: Message, state: FSMContext):
            try:
                parties_text = message.text
                
                # Базовая валидация ввода
                if len(parties_text) < 20 or ':' not in parties_text:
                    await message.answer(
                        "⚠️ Пожалуйста, укажите стороны в формате:\n"
                        "<code>Роль1: Название/ФИО</code>\n"
                        "<code>Роль2: Название/ФИО</code>\n\n"
                        "Пример:\n"
                        "<i>Арендодатель: ООО 'Ромашка'\n"
                        "Арендатор: Иван Иванов</i>"
                    )
                    return
                    
                await state.update_data(parties_text=parties_text)
                
                # Извлекаем информацию о сторонах
                async with self.show_loading(message.chat.id, Chat