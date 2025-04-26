import logging
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from aiogram import F
from aiogram.fsm.context import FSMContext
from utils.settings import BOT_TOKEN
from utils.prompts import TEXT_COLLECTING, TEXT_CLARIFYING

# Инициализация бота и диспетчера
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Состояния FSM
class ReadyDocFSM:
    collecting_data = "collecting_data"
    clarifying_data = "clarifying_data"
    generating_draft = "generating_draft"
    legal_check = "legal_check"
    finalizing_document = "finalizing_document"
    sending_result = "sending_result"

# Шаг 1: Сбор данных
@dp.message(F.text)
async def collect_data(message: Message, state: FSMContext):
    await message.answer(TEXT_COLLECTING)
    user_data = await state.get_data()

    # Пример: собираем название компании
    user_data["company_name"] = message.text  # Просто пример
    await state.update_data(user_data)

    # Переход к уточнению данных
    await message.answer(TEXT_CLARIFYING)
    await collect_clarification(message)

# Шаг 2: Уточнение данных
async def collect_clarification(message: Message):
    user_data = await dp.storage.get_data(message.from_user.id)

    # Пример уточнения (если нужно)
    clarification_question = "Уточните, пожалуйста, информацию."
    await message.answer(clarification_question)

    # Логика уточнения
    if message.text:  # Если есть ответ
        user_data["clarified_info"] = message.text

    # Переход к генерации черновика
    await message.answer("Генерируем черновик...")
    await generate_draft(message)

# Шаг 3: Генерация документа
async def generate_draft(message: Message):
    user_data = await dp.storage.get_data(message.from_user.id)

    # Пример генерации документа (замени на реальную логику)
    document_text = f"Документ для компании {user_data.get('company_name')}."

    # Переход к юридической проверке
    await message.answer("Проверяем юридическую актуальность документа...")
    await legal_check(message, document_text)

# Шаг 4: Юридическая проверка
async def legal_check(message: Message, document_text: str):
    # Пример проверки (замени на реальную логику)
    legal_issues = "Нет проблем с законом."  # Это заглушка

    if legal_issues:
        # Если есть проблемы, отправляем уведомление
        await message.answer(f"Проблемы: {legal_issues}")
        await message.answer("Начинаю исправления...")
        await fix_issues(message)
    else:
        # Если всё в порядке, финализируем документ
        await message.answer("Документ в порядке.")
        await finalize_document(message, document_text)

# Шаг 5: Исправление проблем
async def fix_issues(message: Message):
    # Логика исправления (если нужно)
    await message.answer("Исправляю...")
    await generate_draft(message)  # Повторная генерация с исправлениями

# Шаг 6: Финализация и отправка документа
async def finalize_document(message: Message, document_text: str):
    # Генерация .docx файла (это заглушка, замените на реальную логику)
    doc_file = document_text  # Пока просто текст

    # Отправка документа пользователю
    await message.answer("Ваш документ готов!")
    await message.answer_document(doc_file)
    
    # Завершаем процесс
    await message.answer("Спасибо за использование нашего сервиса.")

# Основной цикл
async def main():
    # Запуск обработки сообщений
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
