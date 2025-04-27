import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardRemove
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
import openai
import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройки логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
bot = Bot(token=os.getenv('BOT_TOKEN'))
dp = Dispatcher(bot, storage=MemoryStorage())

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
@dp.message_handler(commands='start')
async def cmd_start(message: types.Message):
    await message.answer(
        "Здравствуйте! Какой документ Вам нужен?",
        reply_markup=ReplyKeyboardRemove()
    )
    await DocumentCreation.waiting_for_document_type.set()

# Обработка типа документа
@dp.message_handler(state=DocumentCreation.waiting_for_document_type)
async def process_document_type(message: types.Message, state: FSMContext):
    await state.update_data(document_type=message.text)
    await message.answer("Кто стороны документа?")
    await DocumentCreation.next()

# Обработка сторон
@dp.message_handler(state=DocumentCreation.waiting_for_parties)
async def process_parties(message: types.Message, state: FSMContext):
    await state.update_data(parties=message.text)
    await message.answer("Какова цель документа?")
    await DocumentCreation.next()

# Обработка цели
@dp.message_handler(state=DocumentCreation.waiting_for_purpose)
async def process_purpose(message: types.Message, state: FSMContext):
    await state.update_data(purpose=message.text)
    await message.answer("Какие ключевые условия должны быть учтены?")
    await DocumentCreation.next()

# Обработка ключевых условий
@dp.message_handler(state=DocumentCreation.waiting_for_key_terms)
async def process_key_terms(message: types.Message, state: FSMContext):
    await state.update_data(key_terms=message.text)
    await message.answer("Есть ли особые требования или пожелания?")
    await DocumentCreation.next()

# Обработка особых требований и генерация документа
@dp.message_handler(state=DocumentCreation.waiting_for_special_requirements)
async def process_special_requirements(message: types.Message, state: FSMContext):
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
    
    await state.finish()

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
if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
