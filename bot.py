import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from utils.settings import BOT_TOKEN
from utils.prompts import *
from utils.gpt_text_gen import gpt_generate_text, gpt_check_missing_data
from utils.legal_checker import check_document_legality
from utils.docgen import generate_docx

logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
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
async def collect_data(message: Message):
    # Пример: собираем название компании
    await message.answer(TEXT_COLLECTING)
    user_data = await dp.storage.get_data(message.from_user.id)
    
    # Здесь логика для сбора данных (например, название компании)
    user_data["company_name"] = message.text  # Просто пример
    await dp.storage.set_data(message.from_user.id, user_data)

    # Переход к уточнению данных
    await message.answer(TEXT_CLARIFYING)
    await collect_clarification(message)

# Шаг 2: Уточнение данных
async def collect_clarification(message: Message):
    user_data = await dp.storage.get_data(message.from_user.id)

    # Пример уточнения (если нужно)
    clarification_question = TEXT_CLARIFICATION
    await message.answer(clarification_question)

    # Здесь логика уточнения
    if message.text:  # Если есть ответ
        user_data["clarified_info"] = message.text

    # Переход к генерации черновика
    await message.answer(TEXT_GENERATING)
    await generate_draft(message)

# Шаг 3: Генерация документа
async def generate_draft(message: Message):
    user_data = await dp.storage.get_data(message.from_user.id)

    # Генерация текста документа через GPT
    document_text = await gpt_generate_text(user_data)

    # Переход к юридической проверке
    await message.answer(TEXT_CHECKING_LEGALITY)
    await legal_check(message, document_text)

# Шаг 4: Юридическая проверка
async def legal_check(message: Message, document_text: str):
    legal_issues = await check_document_legality(document_text)

    if legal_issues:
        # Если есть проблемы, отправляем уведомление
        await message.answer(TEXT_LEGAL_ISSUES.format(issues=legal_issues))
        await message.answer(TEXT_FIX_ISSUES)
        await fix_issues(message)
    else:
        # Если всё в порядке, финализируем документ
        await message.answer(TEXT_DOCUMENT_OK)
        await finalize_document(message, document_text)

# Шаг 5: Исправление проблем
async def fix_issues(message: Message):
    # Логика исправления (если нужно)
    await message.answer(TEXT_FIXING)
    await generate_draft(message)  # Повторная генерация с исправлениями

# Шаг 6: Финализация и отправка документа
async def finalize_document(message: Message, document_text: str):
    # Генерация .docx файла
    doc_file = await generate_docx(document_text)

    # Отправка документа пользователю
    await message.answer(TEXT_DOCUMENT_READY)
    await message.answer_document(doc_file)
    
    # Завершаем процесс
    await message.answer(TEXT_THANKS)

async def main():
    # Основной цикл
    dp.message_handler(lambda message: True)(collect_data)  # Пример для начала
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
