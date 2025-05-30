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
            current_variable = State()  # Состояние для ввода переменных
            document_review = State()
            waiting_for_special_terms = State()
            waiting_for_additional_clauses = State()  # Дополнительные условия
            parties_confirmation = State()  # Подтверждение сторон
        
        self.states = DocGenState
        self.register_handlers()

    def extract_entities(self, text: str) -> dict:
        doc = Doc(text)
        doc.segment(self.segmenter)
        doc.tag_ner(self.ner_tagger)
        
        organisations = []
        persons = []
        
        for span in doc.spans:
            if span.type == "ORG":
                org_name = span.normal if span.normal else span.text
                organisations.append(org_name)
            elif span.type == "PER":
                person_name = span.normal if span.normal else span.text
                persons.append(person_name)
        
        return {
            'organisations': organisations,
            'persons': persons
        }

    async def identify_roles(self, document_text: str) -> dict:
        try:
            response = await self.generate_gpt_response(
                system_prompt="""Ты юридический ассистент. Определи переменные, которые нужно заполнить в договоре.
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
                    },
                    "variables": ["АДРЕС_ОБЪЕКТА", "ПЛОЩАДЬ", "КАДАСТРОВЫЙ_НОМЕР", "АРЕНДНАЯ_ПЛАТА", "СРОК_АРЕНДЫ", 
                                  "ДАТА_НАЧАЛА", "ДАТА_ОКОНЧАНИЯ", "СТАВКА_НДС", "ДЕПОЗИТ", "КОММУНАЛЬНЫЕ_ПЛАТЕЖИ"]
                }""",
                user_prompt=f"Документ:\n{document_text}",
                chat_id=None
            )
            
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group(0))
                # Добавляем стандартные переменные, если их нет
                if "variables" not in result:
                    result["variables"] = []
                standard_vars = ["АДРЕС_ОБЪЕКТА", "ПЛОЩАДЬ", "КАДАСТРОВЫЙ_НОМЕР", "АРЕНДНАЯ_ПЛАТА", 
                                 "СРОК_АРЕНДЫ", "ДАТА_НАЧАЛА", "ДАТА_ОКОНЧАНИЯ", "СТАВКА_НДС", 
                                 "ДЕПОЗИТ", "КОММУНАЛЬНЫЕ_ПЛАТЕЖИ"]
                for var in standard_vars:
                    if var not in result["variables"]:
                        result["variables"].append(var)
                return result
            return {"roles": {}, "field_descriptions": {}, "variables": []}
        except Exception as e:
            logger.error("Ошибка определения ролей: %s", e)
            return {"roles": {}, "field_descriptions": {}, "variables": []}

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
                await state.update_data(parties_text=parties_text)
                
                # Извлекаем информацию о сторонах
                async with self.show_loading(message.chat.id, ChatAction.TYPING):
                    parties_info = await self.extract_parties_info(parties_text)
                    
                    # Проверяем, что есть минимум две стороны
                    if len(parties_info.get("parties", [])) < 2:
                        await message.answer(
                            "⚠️ Не удалось определить две стороны договора.\n"
                            "Пожалуйста, укажите стороны в одном из форматов:\n\n"
                            "<b>Вариант 1:</b>\n"
                            "<i>Арендодатель: ФИО/Название\n"
                            "Арендатор: ФИО/Название</i>\n\n"
                            "<b>Вариант 2:</b>\n"
                            "<i>Арендодатель - ФИО/Название\n"
                            "Арендатор - ФИО/Название</i>\n\n"
                            "<b>Вариант 3:</b>\n"
                            "<i>Арендодатель: ФИО/Название\n"
                            "Арендатор: ФИО/Название</i>"
                        )
                        return
                        
                    # Показываем пользователю, как мы поняли стороны
                    confirmation = "✅ Определены стороны договора:\n"
                    for party in parties_info["parties"]:
                        confirmation += f"<b>{party['role']}:</b> {party['name']} ({party['type']})\n"
                    
                    await message.answer(confirmation)
                    
                    # Предлагаем исправить, если неверно
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✅ Да, верно", callback_data="parties_confirm"),
                            InlineKeyboardButton(text="✏️ Нет, исправить", callback_data="parties_correct")
                        ]
                    ])
                    
                    await message.answer(
                        "Верно ли определены стороны?",
                        reply_markup=keyboard
                    )
                    
                    await state.update_data(parties_info=parties_info)
                    await state.set_state(self.states.parties_confirmation)
                
            except Exception as e:
                logger.error("Ошибка обработки информации о сторонах: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки информации о сторонах. Попробуйте снова.")
                await state.clear()

        @self.dp.callback_query(F.data == "parties_confirm")
        async def handle_parties_confirm(callback: types.CallbackQuery, state: FSMContext):
            await callback.message.delete()
            data = await state.get_data()
            
            # Генерируем черновик с учетом информации о сторонах
            await callback.message.answer("🧠 Генерирую договор аренды...")
            
            # Формируем промпт с учетом специализации
            rental_params = data.get('rental_params', {})
            business_type = data.get('business_type', 'other')
            
            # Добавляем специфичные условия для разных типов бизнеса
            business_specific = ""
            if business_type == "cafe":
                business_specific = (
                    "ДОБАВЬ СПЕЦИФИЧНЫЕ УСЛОВИЯ ДЛЯ ОБЩЕПИТА:\n"
                    " - Требования СЭС\n - Правила пожарной безопасности\n"
                    " - Утилизация отходов\n - График поставок продуктов\n"
                )
            elif business_type == "shop":
                business_specific = (
                    "ДОБАВЬ СПЕЦИФИЧНЫЕ УСЛОВИЯ ДЛЯ МАГАЗИНОВ:\n"
                    " - Режим работы\n - Требования к витринам\n"
                    " - Ответственность за кражу товара\n"
                )
            elif business_type == "beauty":
                business_specific = (
                    "ДОБАВЬ СПЕЦИФИЧНЫЕ УСЛОВИЯ ДЛЯ САЛОНОВ КРАСОТЫ:\n"
                    " - Санитарные нормы\n - Лицензии на процедуры\n"
                    " - Утилизация расходных материалов\n"
                )
            
            rent_specific_prompt = f"""
            Дополнительные параметры аренды:
            - Тип помещения: {rental_params.get('property_type', 'не указан')}
            - Площадь: {rental_params.get('area', 'не указана')} м²
            - Мебель/техника: {rental_params.get('furnished', 'не указано')}
            - Налогообложение арендодателя: {rental_params.get('tax_system', 'не указано')}
            {business_specific}
            """
            
            document = await self.generate_gpt_response(
                system_prompt=f"""Ты юрист, специализирующийся на аренде коммерческой недвижимости. 
                Составь юридически корректный договор аренды с учетом:
                1. Статьи 606-625 ГК РФ
                2. Особенностей налогообложения: {rental_params.get('tax_system', '')}
                3. Требований к коммерческой аренде
                4. Практики аренды в России
                {rent_specific_prompt}
                
                ВАЖНО: Для всех переменных данных используй ТОЛЬКО плейсхолдеры в квадратных скобках в формате [НАЗВАНИЕ_ПЕРЕМЕННОЙ]. 
                Никогда не используй фразы вроде "Указать дату окончания" - вместо этого используй [ДАТА_ОКОНЧАНИЯ].
                
                КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:
                1. Для физических лиц: указать "действующий от своего имени" и паспортные данные
                2. Для ИП: указать "ИП [ФИО], действующий на основании свидетельства ОГРНИП"
                3. Для ООО: указать "в лице [ДОЛЖНОСТЬ] [ФИО], действующего на основании устава"
                4. В предмете договора обязательно указать:
                   - Точный адрес с номером помещения: [АДРЕС_ОБЪЕКТА]
                   - Площадь помещения: [ПЛОЩАДЬ] м²
                   - Кадастровый номер: [КАДАСТРОВЫЙ_НОМЕР]
                5. В арендной плате:
                   - Указать валюту (рубли)
                   - Уточнить включен ли НДС (для ОСН - обязательно): [СТАВКА_НДС]
                   - Прописать сумму прописью: [АРЕНДНАЯ_ПЛАТА_ПРОПИСЬЮ]
                6. Добавить разделы:
                   - Коммунальные платежи: [КОММУНАЛЬНЫЕ_ПЛАТЕЖИ]
                   - Порядок расторжения
                   - Реквизиты сторон
                   - Ответственность сторон
                   - Форс-мажор
                7. В подписях указать:
                   - Для ИП: ИНН и ОГРНИП
                   - Для физлиц: паспортные данные
                   - Для ООО: ИНН, ОГРН, КПП
                8. Проверить согласованность дат:
                   - Дата начала: [ДАТА_НАЧАЛА]
                   - Дата окончания: [ДАТА_ОКОНЧАНИЯ]""",
                user_prompt=(
                    f"Описание документа:\n{data['initial_text']}\n\n"
                    f"Стороны договора:\n{data['parties_text']}"
                ),
                chat_id=callback.message.chat.id
            )

            filename = f"draft_{callback.message.from_user.id}.docx"
            path = self.save_docx(document, filename)
            
            await state.update_data(document_text=document)
            await callback.message.answer_document(FSInputFile(path))
            await callback.message.answer(
                "📄 Черновик договора готов! Теперь заполним обязательные реквизиты."
            )
            await self.start_variable_filling(callback.message, state)

        @self.dp.callback_query(F.data == "parties_correct")
        async def handle_parties_correct(callback: types.CallbackQuery, state: FSMContext):
            await callback.message.delete()
            await callback.message.answer(
                "✏️ <b>Пожалуйста, укажите стороны договора в формате:</b>\n\n"
                "<code>Арендодатель: ФИО/Название</code>\n"
                "<code>Арендатор: ФИО/Название</code>\n\n"
                "Или просто перечислите стороны через запятую:"
            )
            await state.set_state(self.states.waiting_for_parties_info)

        @self.dp.callback_query(F.data == "add_clauses")
        async def handle_add_clauses(callback: types.CallbackQuery, state: FSMContext):
            await callback.message.answer(
                "📝 <b>Выберите дополнительные условия для договора:</b>\n\n"
                "1. Право субаренды\n"
                "2. Автоматическая пролонгация\n"
                "3. Право выкупа помещения\n"
                "4. Условие о ремонте\n"
                "5. Страхование имущества\n\n"
                "Укажите номера нужных условий через запятую (например: 1,3,5)"
            )
            await state.set_state(self.states.waiting_for_additional_clauses)

        @self.dp.message(self.states.waiting_for_additional_clauses)
        async def handle_additional_clauses(message: Message, state: FSMContext):
            try:
                selected_clauses = message.text
                await state.update_data(additional_clauses=selected_clauses)
                
                data = await state.get_data()
                base_text = data.get("final_document", "")
                
                async with self.show_loading(message.chat.id, ChatAction.TYPING):
                    await message.answer("🔧 Добавляю выбранные условия в договор...")
                    updated_doc = await self.generate_gpt_response(
                        system_prompt="Ты юридический редактор. Добавь в договор аренды выбранные условия.",
                        user_prompt=f"Добавь условия: {selected_clauses}\n\nДокумент:\n{base_text}",
                        chat_id=message.chat.id
                    )

                filename = f"updated_{message.from_user.id}.docx"
                path = self.save_docx(updated_doc, filename)
                
                await message.answer_document(FSInputFile(path))
                await message.answer("✅ Договор обновлен с учетом выбранных условий!")
                
                # Обновляем финальный документ
                await state.update_data(final_document=updated_doc)
                
                # Предлагаем финальные действия
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Завершить", callback_data="confirm_document"),
                        InlineKeyboardButton(text="✏️ Добавить свои условия", callback_data="add_terms")
                    ]
                ])
                
                await message.answer(
                    "Вы можете:\n"
                    "- Завершить и получить финальную версию\n"
                    "- Добавить свои особые условия",
                    reply_markup=keyboard
                )
                    
            except Exception as e:
                logger.error("Ошибка обработки: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
                await state.set_state(self.states.document_review)

        # НОВЫЙ ОБРАБОТЧИК ДЛЯ ВВОДА ПЕРЕМЕННЫХ
        @self.dp.message(self.states.current_variable)
        async def handle_variable_input(message: Message, state: FSMContext):
            try:
                data = await state.get_data()
                current_var = data['current_variable']
                user_input = message.text
                
                # Проверка ввода для специфичных полей
                if "ПЛОЩАДЬ" in current_var:
                    if not user_input.isdigit():
                        await message.answer("⚠️ Площадь должна быть числом. Укажите число в квадратных метрах:")
                        return
                elif "ИНН" in current_var:
                    if not self.validate_inn(user_input):
                        await message.answer("⚠️ ИНН должен содержать 10 или 12 цифр. Введите корректный ИНН:")
                        return
                elif "ДАТА" in current_var:
                    if not re.match(r'\d{2}\.\d{2}\.\d{4}', user_input):
                        await message.answer("⚠️ Дата должна быть в формате ДД.ММ.ГГГГ. Введите корректную дату:")
                        return
                elif "АРЕНДНАЯ_ПЛАТА" in current_var or "ДЕПОЗИТ" in current_var:
                    if not user_input.isdigit():
                        await message.answer("⚠️ Сумма должна быть числом. Укажите сумму в рублях:")
                        return
                
                # Сохраняем введенное значение
                filled = data.get('filled_variables', {})
                filled[current_var] = user_input
                
                # Обновляем индекс текущей переменной
                await state.update_data(
                    filled_variables=filled,
                    current_variable_index=data['current_variable_index'] + 1
                )
                
                # Переходим к следующей переменной
                await self.ask_next_variable(message, state)
                
            except Exception as e:
                logger.error("Ошибка обработки ввода переменной: %s\n%s", e, traceback.format_exc())
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
                await state.clear()

        # НОВЫЕ ОБРАБОТЧИКИ ДЛЯ КНОПОК
        @self.dp.callback_query(F.data == "confirm_document")
        async def handle_confirm_document(callback: types.CallbackQuery, state: FSMContext):
            try:
                await callback.message.delete()
                await self.send_final_document(callback.message, state)
            except Exception as e:
                logger.error("Ошибка подтверждения документа: %s", e)
                await callback.message.answer("⚠️ Ошибка завершения оформления. Попробуйте снова.")

        @self.dp.callback_query(F.data == "add_terms")
        async def handle_add_terms(callback: types.CallbackQuery, state: FSMContext):
            await callback.message.answer(
                "✏️ Введите ваши особые условия, которые нужно добавить в договор:"
            )
            await state.set_state(self.states.waiting_for_special_terms)

        @self.dp.message(self.states.waiting_for_special_terms)
        async def handle_special_terms(message: Message, state: FSMContext):
            try:
                custom_terms = message.text
                data = await state.get_data()
                base_text = data.get("final_document", "")
                
                async with self.show_loading(message.chat.id, ChatAction.TYPING):
                    updated_doc = await self.generate_gpt_response(
                        system_prompt="Ты юридический редактор. Добавь в договор аренды пользовательские условия.",
                        user_prompt=f"Добавь эти особые условия: {custom_terms}\n\nВ текущий договор:\n{base_text}",
                        chat_id=message.chat.id
                    )

                filename = f"custom_{message.from_user.id}.docx"
                path = self.save_docx(updated_doc, filename)
                
                await message.answer_document(FSInputFile(path))
                await message.answer("✅ Особые условия добавлены в договор!")
                
                # Обновляем документ в состоянии
                await state.update_data(final_document=updated_doc)
                
                # Предлагаем финальные действия
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Завершить", callback_data="confirm_document")]
                ])
                await message.answer("Документ обновлен. Вы можете завершить оформление.", reply_markup=keyboard)
                    
            except Exception as e:
                logger.error("Ошибка добавления условий: %s", e)
                await message.answer("⚠️ Ошибка обработки. Попробуйте снова.")
                await state.set_state(self.states.document_review)

    async def extract_rental_params(self, text: str) -> dict:
        """Извлекает структурированные параметры аренды из текста с резервной логикой"""
        try:
            response = await self.generate_gpt_response(
                system_prompt="""Ты специалист по аренде недвижимости. Извлеки параметры:
                Ответ в JSON:
                {
                    "property_type": "тип помещения (офисное, торговое, производственное, складское, жилое)",
                    "area": "площадь в кв.м",
                    "furnished": "мебель/техника (да/нет)",
                    "tax_system": "система налогообложения (ОСН/УСН/Патент)",
                    "deposit": "сумма депозита",
                    "utilities": "коммунальные платежи (включены/отдельно)",
                    "sublease": "субаренда (разрешена/запрещена)",
                    "address": "адрес (если указан)"
                }""",
                user_prompt=text,
                chat_id=None
            )
            
            # Пытаемся распарсить JSON
            try:
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    result = json.loads(json_match.group(0))
                    
                    # Проверка и дополнение критических параметров
                    if not result.get("property_type"):
                        # Эвристика для определения типа помещения
                        if any(kw in text.lower() for kw in ["офис", "офисное"]):
                            result["property_type"] = "офисное"
                        elif any(kw in text.lower() for kw in ["магазин", "торгов", "бутик"]):
                            result["property_type"] = "торговое"
                        elif any(kw in text.lower() for kw in ["производств", "цех"]):
                            result["property_type"] = "производственное"
                        elif any(kw in text.lower() for kw in ["склад"]):
                            result["property_type"] = "складское"
                        else:
                            result["property_type"] = "не указано"
                    
                    if not result.get("area"):
                        # Резервное извлечение площади через регулярные выражения
                        area_match = re.search(r'(\d+)\s*(м²|м2|кв\.?м|кв|м\s*кв)', text)
                        if area_match:
                            result["area"] = area_match.group(1)
                    
                    return result
            except json.JSONDecodeError:
                logger.warning(f"Невалидный JSON: {response}")
        
        except Exception as e:
            logger.error(f"Ошибка извлечения параметров: {e}")
        
        # Fallback: возвращаем пустой словарь
        return {}

    async def extract_parties_info(self, text: str) -> dict:
        """Извлекает структурированную информацию о сторонах договора с улучшенным определением ролей"""
        # Нормализуем текст: приводим к нижнему регистру, убираем лишние пробелы
        normalized_text = re.sub(r'\s+', ' ', text.lower()).strip()
        
        # Определяем роли по ключевым словам
        landlord_keywords = ["арендодатель", "собственник", "лизингодатель", "арендодателя", "owner", "lessor"]
        tenant_keywords = ["арендатор", "наниматель", "лизингополучатель", "арендатора", "tenant", "lessee"]
        
        # Пытаемся найти роли в тексте
        landlord = None
        tenant = None
        
        # Ищем арендодателя
        for keyword in landlord_keywords:
            if keyword in normalized_text:
                # Извлекаем текст после ключевого слова
                match = re.search(fr"{keyword}[:\-—\s]*(.+?)(?:{tenant_keywords[0]}|$)", normalized_text)
                if match:
                    landlord = match.group(1).strip()
                    break
        
        # Ищем арендатора
        for keyword in tenant_keywords:
            if keyword in normalized_text:
                match = re.search(fr"{keyword}[:\-—\s]*(.+)", normalized_text)
                if match:
                    tenant = match.group(1).strip()
                    break
        
        # Если не нашли по ключевым словам, используем эвристику: первое упомянутое лицо - арендодатель
        if not landlord or not tenant:
            # Используем Natasha для извлечения сущностей
            entities = self.extract_entities(text)
            
            # Объединяем организации и персоны
            all_entities = entities['organisations'] + entities['persons']
            
            if len(all_entities) >= 2:
                landlord = all_entities[0]
                tenant = all_entities[1]
            elif len(all_entities) == 1:
                landlord = all_entities[0]
                tenant = "Не определено"
            else:
                # Резервный метод: разделяем по запятым или союзам
                parts = re.split(r'[,;и]| арендодатель | арендатор ', text, flags=re.IGNORECASE)
                parts = [p.strip() for p in parts if p.strip()]
                
                if len(parts) >= 2:
                    landlord = parts[0]
                    tenant = parts[1]
                elif len(parts) == 1:
                    landlord = parts[0]
                    tenant = "Не определено"
                else:
                    landlord = "Не указано"
                    tenant = "Не указано"
    
        # Определяем тип лица
        landlord_type = self.detect_party_type(landlord) if landlord else "не определен"
        tenant_type = self.detect_party_type(tenant) if tenant else "не определен"
        
        return {
            "parties": [
                {
                    "role": "Арендодатель",
                    "type": landlord_type,
                    "name": landlord or "Не указано",
                    "details": ""
                },
                {
                    "role": "Арендатор",
                    "type": tenant_type,
                    "name": tenant or "Не указано",
                    "details": ""
                }
            ]
        }

    def detect_party_type(self, text: str) -> str:
        """Определяет тип лица по тексту с большей точностью"""
        if not text:
            return "не определен"
        
        text_lower = text.lower()
        
        # Список юридических форм с приоритетом
        legal_forms = {
            "ип": "ИП",
            "индивидуальный предприниматель": "ИП",
            "ооо": "ООО",
            "общество с ограниченной ответственностью": "ООО",
            "ао": "АО",
            "оао": "АО",
            "зао": "АО",
            "пао": "АО",
            "акционерное общество": "АО",
            "публичное акционерное общество": "АО",
            "закрытое акционерное общество": "АО",
            "нко": "НКО",
            "некоммерческая организация": "НКО",
            "пк": "ПК",
            "производственный кооператив": "ПК",
            "кфх": "КФХ",
            "крестьянское фермерское хозяйство": "КФХ",
            "тсн": "ТСН",
            "товарищество собственников недвижимости": "ТСН",
            "потребительский кооператив": "ПК",
            "адвокатское образование": "АО",
            "адвокатское бюро": "АО",
            "коллегия адвокатов": "КА",
            "юридическая компания": "ЮК",
            "юридическое лицо": "ЮЛ",
        }
        
        # Проверка явных указаний юридической формы
        for pattern, form in legal_forms.items():
            if re.search(rf"\b{pattern}\b", text_lower):
                return form
        
        # Физические лица - явные указания
        if re.search(r"\bфл\b|\bфиз\b|\bгражданин\b|\bг-н\b|\bфизлицо\b", text_lower):
            return "физическое лицо"
        
        # Физические лица - по формату ФИО
        # Формат: Фамилия Имя Отчество или Фамилия И.О.
        if (re.search(r"\b[а-яё]+\s+[а-яё][.\s]*[а-яё]?[.\s]*[а-яё]?[.]?\b", text_lower) or
            re.search(r"[А-ЯЁ]\.[\s]*[А-ЯЁ]\.[\s]*[А-ЯЁ][а-яё]+", text)):
            return "физическое лицо"
        
        # Физические лица - эвристика по структуре имени
        words = text.split()
        if len(words) >= 2:
            # Если все слова начинаются с заглавной буквы и состоят только из букв
            if all(word.istitle() and word.isalpha() for word in words):
                return "физическое лицо"
        
        # Проверка по ИНН (10 или 12 цифр)
        inn_match = re.search(r"\b\d{10,12}\b", text)
        if inn_match:
            inn = inn_match.group(0)
            if len(inn) == 10:
                return "ЮЛ"  # Юридическое лицо
            elif len(inn) == 12:
                return "ИП"  # Индивидуальный предприниматель
        
        # По умолчанию считаем юридическим лицом
        return "ЮЛ"

    async def start_variable_filling(self, message: Message, state: FSMContext):
        data = await state.get_data()
        document_text = data['document_text']
        role_info = await self.identify_roles(document_text)
        
        # Извлекаем переменные из документа
        raw_vars = list(set(re.findall(r'\[(.*?)\]', document_text)))
        
        # Фильтруем переменные: оставляем только те, которые есть в role_info
        all_vars = []
        for var in raw_vars:
            # Если переменная есть в описаниях или в списке переменных
            if var in role_info.get("field_descriptions", {}) or var in role_info.get("variables", []):
                all_vars.append(var)
        
        # Добавляем обязательные переменные
        required_vars = role_info.get("variables", [])
        for var in required_vars:
            if var not in all_vars:
                all_vars.append(var)
        
        # Для аренды добавляем специфичные поля
        if data.get('is_rental'):
            rent_specific_vars = [
                "АДРЕС_ОБЪЕКТА", "ПЛОЩАДЬ", "КАДАСТРОВЫЙ_НОМЕР",
                "АРЕНДНАЯ_ПЛАТА", "СРОК_АРЕНДЫ", "ДАТА_НАЧАЛА",
                "ДАТА_ОКОНЧАНИЯ", "СТАвКА_НДС", "ДЕПОЗИТ",
                "КОММУНАЛЬНЫЕ_ПЛАТЕЖИ", "ПОРЯДОК_ОПЛАТЫ"
            ]
            for var in rent_specific_vars:
                if var not in all_vars:
                    all_vars.append(var)
                    if var not in role_info["field_descriptions"]:
                        role_info["field_descriptions"][var] = var.replace("_", " ").lower()
                
            # Автоподстановка данных из rental_params
            rental_params = data.get('rental_params', {})
            filled = data.get('filled_variables', {})
            if 'area' in rental_params:
                filled['ПЛОЩАДЬ'] = rental_params['area']
            if 'address' in rental_params:
                filled['АДРЕС_ОБЪЕКТА'] = rental_params['address']
            if 'deposit' in rental_params:
                filled['ДЕПОЗИТ'] = rental_params['deposit']
            if 'utilities' in rental_params:
                filled['КОММУНАЛЬНЫЕ_ПЛАТЕЖИ'] = rental_params['utilities']
            await state.update_data(filled_variables=filled)
        
        # Группируем переменные по ролям
        grouped_vars = {}
        for var in all_vars:
            role = "Общие"
            for role_name, role_data in role_info.get("roles", {}).items():
                if var in role_data.get("fields", []):
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
                var_descriptions[var] = self.map_variable_to_question(var, role_info)
        
        # Затем специфичные для ролей
        for role, vars_list in grouped_vars.items():
            if role == "Общие":
                continue
                
            # Добавляем разделитель
            ordered_vars.append(f"---{role}---")
            var_descriptions[f"---{role}---"] = f"🔹 <b>{role}</b>"
            
            for var in vars_list:
                ordered_vars.append(var)
                var_descriptions[var] = self.map_variable_to_question(var, role_info)
        
        # Логирование всех переменных
        logger.info("Упорядоченные переменные: %s", ordered_vars)
        
        await state.update_data(
            variables=ordered_vars,
            var_descriptions=var_descriptions,
            filled_variables=data.get('filled_variables', {}),
            current_variable_index=0,
            role_info=role_info  # Сохраняем информацию о ролях
        )
        await self.ask_next_variable(message, state)

    async def ask_next_variable(self, message: Message, state: FSMContext):
        data = await state.get_data()
        variables = data['variables']
        index = data['current_variable_index']
        filled = data['filled_variables']
        var_descriptions = data['var_descriptions']
        
        # Пропускаем разделители
        while index < len(variables) and variables[index].startswith("---"):
            index += 1
            
        if index >= len(variables):
            await self.prepare_final_document(message, state)
            return
            
        current_var = variables[index]
        question = var_descriptions.get(current_var, f"✍️ Введите значение для {current_var}:")
        
        # Проверяем, есть ли предзаполненное значение
        if current_var in filled:
            await state.update_data(current_variable_index=index+1)
            await self.ask_next_variable(message, state)
            return
        
        await state.update_data(
            current_variable=current_var,
            current_variable_index=index
        )
        
        # Добавляем валидацию для специфичных полей
        validation_hint = ""
        if "ИНН" in current_var:
            validation_hint = "\n\n⚠️ ИНН должен содержать 10 или 12 цифр"
        elif "ДАТА" in current_var:
            validation_hint = "\n\n⚠️ Укажите дату в формате ДД.ММ.ГГГГ"
        elif "ПЛОЩАДЬ" in current_var:
            validation_hint = "\n\n⚠️ Укажите число в квадратных метрах (например: 35)"
        elif "АРЕНДНАЯ_ПЛАТА" in current_var or "ДЕПОЗИТ" in current_var:
            validation_hint = "\n\n⚠️ Укажите сумму в рублях (например: 50000)"
        
        await message.answer(f"{question}{validation_hint}")
        await state.set_state(self.states.current_variable)  # Устанавливаем состояние для ввода

    async def prepare_final_document(self, message: Message, state: FSMContext):
        try:
            data = await state.get_data()
            document_text = data['document_text']
            filled = data['filled_variables']
            
            # Заменяем плейсхолдеры значениями
            for var, value in filled.items():
                document_text = document_text.replace(f"[{var}]", value)
            
            # Особые преобразования
            if 'АРЕНДНАЯ_ПЛАТА' in filled:
                amount = int(filled['АРЕНДНАЯ_ПЛАТА'])
                document_text = document_text.replace(
                    "[АРЕНДНАЯ_ПЛАТА_ПРОПИСЬЮ]", 
                    f"{amount} ({self.num2words(amount)}) рублей"
                )
            
            # Проверка документа
            async with self.show_loading(message.chat.id, ChatAction.TYPING):
                await message.answer("🔍 Проверяю документ...")
                reviewed_doc = await self.auto_review_and_fix(document_text, message.chat.id)
                
                # Дополнительная проверка, что вернулся именно документ
                if "договор" not in reviewed_doc.lower() and "аренд" not in reviewed_doc.lower():
                    logger.warning("GPT вернул не документ: %s", reviewed_doc[:100])
                    reviewed_doc = document_text  # Используем исходную версию
            
            # Сохраняем финальную версию
            await state.update_data(final_document=reviewed_doc)
            
            # Отправляем пользователю
            filename = f"final_{message.from_user.id}.docx"
            path = self.save_docx(reviewed_doc, filename)
            await message.answer_document(FSInputFile(path))
            await state.set_state(self.states.document_review)
            
            # Предлагаем дополнительные опции
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Завершить", callback_data="confirm_document"),
                    InlineKeyboardButton(text="📝 Добавить условия", callback_data="add_clauses"),
                    InlineKeyboardButton(text="✏️ Свои условия", callback_data="add_terms")
                ]
            ])
            
            await message.answer(
                "📑 Договор готов! Вы можете:\n"
                "- Завершить оформление\n"
                "- Добавить стандартные условия\n"
                "- Добавить свои особые условия",
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error("Ошибка подготовки документа: %s", e)
            await message.answer("⚠️ Ошибка формирования документа. Попробуйте снова.")
            await state.clear()

    async def send_final_document(self, message: Message, state: FSMContext):
        try:
            data = await state.get_data()
            document_text = data.get('final_document', '')
            
            if not document_text:
                await message.answer("⚠️ Ошибка: документ не найден")
                await state.clear()
                return
            
            # Генерируем финальный DOCX
            filename = f"Договор_аренды_{datetime.datetime.now().strftime('%d%m%Y')}.docx"
            final_path = self.save_docx(document_text, filename)
            await message.answer_document(FSInputFile(final_path))
            
            # Для аренды генерируем дополнительные документы
            if data.get('is_rental', False):
                await message.answer("📝 <b>Генерирую дополнительные документы...</b>")
                
                # Акт приема-передачи
                act_text = await self.generate_acceptance_act(data)
                act_path = self.save_docx(act_text, "Акт_приема-передачи.docx")
                await message.answer_document(FSInputFile(act_path))
                
                # Уведомление о расторжении
                termination_text = await self.generate_termination_notice(data)
                term_path = self.save_docx(termination_text, "Уведомление_о_расторжении.docx")
                await message.answer_document(FSInputFile(term_path))
                
                # Дополнительные рекомендации
                await message.answer(
                    "🔔 <b>Рекомендации по договору аренды:</b>\n\n"
                    "1. Подпишите акт приема-передачи при заселении\n"
                    "2. Храните все платежные документы\n"
                    "3. Уведомляйте о расторжении за 1 месяц\n"
                    "4. Проверьте регистрацию договора, если срок > 1 года\n\n"
                    "Для консультации по налогам используйте /tax_help"
                )
            else:
                await message.answer("✅ Документ готов! Рекомендуем проверить его у юриста.")
            
            await message.answer(
                "✅ Документы готовы! Рекомендуем:\n"
                "1. Проверить реквизиты\n"
                "2. Показать юристу\n"
                "3. Сохранить копии"
            )
            await state.clear()

            # Удаляем временные файлы
            if os.path.exists(final_path):
                os.unlink(final_path)
            
            temp_path = data.get('document_path', '')
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

        except Exception as e:
            logger.error("Ошибка отправки документа: %s", e)
            await message.answer("⚠️ Ошибка завершения. Попробуйте начать заново /start")
            await state.clear()

    async def auto_review_and_fix(self, document: str, chat_id: int) -> str:
        try:
            async with self.show_loading(chat_id, ChatAction.TYPING):
                # Уточненный промпт для получения ИСПРАВЛЕННОГО документа
                reviewed = await self.generate_gpt_response(
                    system_prompt="""Ты юрист-арендный эксперт. Проверь договор и ВНЕСИ ПРЯМО В ТЕКСТ следующие исправления:
                    1. Соответствие ст. 606-625 ГК РФ
                    2. Наличие существенных условий: предмет, цена, срок
                    3. Правильность указания реквизитов сторон
                    4. Соответствие налогообложения (УСН/ОСН)
                    5. Наличие условий о капитальном ремонте
                    6. Порядок расторжения
                    7. Условия о субаренде
                    8. Порядок внесения изменений
                    9. Условия о коммунальных платежах
                    10. Порядок возврата депозита
                    
                    ВАЖНО: Верни ПОЛНЫЙ ИСПРАВЛЕННЫЙ ТЕКСТ ДОГОВОРА, а не описание изменений.
                    Сохрани все плейсхолдеры вида [ПЕРЕМЕННАЯ] нетронутыми.""",
                    user_prompt=f"Вот договор для исправления:\n\n{document}",
                    chat_id=chat_id
                )
            
            # Извлекаем текст договора из возможного форматированного ответа
            if "```" in reviewed:
                reviewed = reviewed.split("```")[1]
            return reviewed.strip()
        
        except Exception as e:
            logger.error("Ошибка проверки: %s", e)
            return document

    async def generate_acceptance_act(self, data: dict) -> str:
        return await self.generate_gpt_response(
            system_prompt="""Ты юрист. Сгенерируй акт приема-передачи помещения к договору аренды.
            Укажи:
            1. Дату и место составления
            2. Ссылку на договор аренды
            3. Описание передаваемого помещения
            4. Состояние помещения и оборудования
            5. Подписи сторон""",
            user_prompt=f"""
            Данные договора:
            {data.get('final_document', '')}
            """,
            chat_id=None
        )

    async def generate_termination_notice(self, data: dict) -> str:
        return await self.generate_gpt_response(
            system_prompt="""Ты юрист. Сгенерируй уведомление о расторжении договора аренды.
            Включи:
            1. Реквизиты сторон
            2. Ссылку на договор
            3. Дату расторжения
            4. Причину расторжения (если требуется)
            5. Порядок возврата депозита
            6. Подпись""",
            user_prompt=f"""
            Данные договора:
            {data.get('final_document', '')}
            """,
            chat_id=None
        )

    async def generate_gpt_response(self, system_prompt: str, user_prompt: str, chat_id: int) -> str:
        try:
            if chat_id:
                async with self.show_loading(chat_id, ChatAction.TYPING):
                    response = await self.openai_client.chat.completions.create(
                        model="gpt-3.5-turbo-0125",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        temperature=0.2,
                        max_tokens=3000
                    )
            else:
                response = await self.openai_client.chat.completions.create(
                    model="gpt-3.5-turbo-0125",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.2,
                    max_tokens=3000
                )
                
            return response.choices[0].message.content.strip()
        
        except httpx.ReadTimeout:
            logger.error("Timeout при запросе к OpenAI")
            return "❌ Превышено время ожидания ответа. Попробуйте позже."
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

    # Метод run для запуска бота
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