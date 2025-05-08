import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from dotenv import load_dotenv
import openai

# Загрузка переменных окружения
load_dotenv()

# Настройки логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота
bot = Bot(token=os.getenv('BOT_TOKEN'), parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Инициализация OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Определение состояний
class DocumentCreation(StatesGroup):
    waiting_for_document_type = State()
    waiting_for_parties = State()
    waiting_for_purpose = State()
    waiting_for_key_terms = State()
    waiting_for_special_requirements = State()

# Стартовая команда
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await message.answer(
        "Здравствуйте! Какой документ Вам нужен?"
    )
    await state.set_state(DocumentCreation.waiting_for_document_type)

# Обработка типа документа
@router.message(DocumentCreation.waiting_for_document_type)
async def process_document_type(message: Message, state: FSMContext):
    await state.update_data(document_type=message.text)
    await message.answer("""**Приветствуем вас!**
Давайте за несколько вопросов подготовим юридически корректный документ.
Всё просто: отвечайте в свободной форме.""")
    await state.set_state(DocumentCreation.waiting_for_parties)

# Обработка сторон
@router.message(DocumentCreation.waiting_for_parties)
async def process_parties(message: Message, state: FSMContext):
    await state.update_data(parties=message.text)
    await message.answer("Какова цель документа?")
    await state.set_state(DocumentCreation.waiting_for_purpose)

# Обработка цели
@router.message(DocumentCreation.waiting_for_purpose)
async def process_purpose(message: Message, state: FSMContext):
    await state.update_data(purpose=message.text)
    await message.answer("Какие ключевые условия должны быть учтены?")
    await state.set_state(DocumentCreation.waiting_for_key_terms)

# Обработка ключевых условий
@router.message(DocumentCreation.waiting_for_key_terms)
async def process_key_terms(message: Message, state: FSMContext):
    await state.update_data(key_terms=message.text)
    await message.answer("Есть ли особые требования или пожелания?")
    await state.set_state(DocumentCreation.waiting_for_special_requirements)

# Обработка особых требований и генерация документа
@router.message(DocumentCreation.waiting_for_special_requirements)
async def process_special_requirements(message: Message, state: FSMContext):
    await state.update_data(special_requirements=message.text)
    user_data = await state.get_data()

    # Формирование промта для OpenAI
    prompt = (
        f"Создай юридический документ на русском языке, соответствующий законодательству РФ.\n\n"
        f"Тип документа: {user_data['document_type']}\n"
        f"Стороны: {user_data['parties']}\n"
        f"Цель документа: {user_data['purpose']}\n"
        f"Ключевые условия: {user_data['key_terms']}\n"
        f"Особые требования: {user_data['special_requirements']}\n\n"
        f"Документ должен быть официальным, юридически грамотным и готовым для использования."
    )

    await message.answer("Генерирую документ, пожалуйста, подождите...")

    try:
        response = await generate_document(prompt)
        await message.answer(response)
    except Exception as e:
        logging.error(f"Ошибка при генерации документа: {e}")
        await message.answer("Произошла ошибка при генерации документа. Попробуйте позже.")

    await state.clear()

# Функция генерации документа через OpenAI
async def generate_document(prompt: str) -> str:
    completion = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Ты опытный юрист, создающий юридические документы."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=3000
    )
    return completion.choices[0].message.content.strip()

# Запуск бота
async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())