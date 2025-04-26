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

from dotenv import load_dotenv  # <-- ДОБАВЛЕНО

# Загрузка переменных окружения
load_dotenv()  # <-- ДОБАВЛЕНО

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

# Начало процесса: сбор данных
async def collect_data(message: Message):
    await message.answer(TEXT_COLLECTING)
    user_data = {"initial_request": message.text}
    await dp.storage.set_data(message.from_user.id, user_data)

    await ask_clarifications(message)

# Уточнения — только необходимые
async def ask_clarifications(message: Message):
    user_data = await dp.storage.get_data(message.from_user.id)

    clarifications_needed = await gpt_check_missing_data(user_data["initial_request"])

    if clarifications_needed:
        max_questions = 6
        questions = clarifications_needed[:max_questions]
        user_data["clarifications"] = {}

        for question in questions:
            await message.answer(question)
            user_response = message.text or "нет данных"
            user_data["clarifications"][question] = user_response

        await dp.storage.set_data(message.from_user.id, user_data)

    await generate_document(message)

# Генерация черновика документа
async def generate_document(message: Message):
    user_data = await dp.storage.get_data(message.from_user.id)

    await message.answer(TEXT_GENERATING)

    document_text = await gpt_generate_text(user_data)

    await check_legality(message, document_text)

# Проверка юридической корректности
async def check_legality(message: Message, document_text: str):
    await message.answer(TEXT_CHECKING_LEGALITY)

    legal_issues = await check_document_legality(document_text)

    if legal_issues:
        await message.answer(TEXT_LEGAL_ISSUES.format(issues=legal_issues))
        await message.answer(TEXT_FIX_ISSUES)
        await fix_issues(message)
    else:
        await finalize_document(message, document_text)

# Исправление ошибок, если они есть
async def fix_issues(message: Message):
    await message.answer(TEXT_FIXING)
    await generate_document(message)

# Финализация и отправка готового документа
async def finalize_document(message: Message, document_text: str):
    doc_file = await generate_docx(document_text)

    await message.answer(TEXT_DOCUMENT_READY)
    await message.answer_document(doc_file)
    await message.answer(TEXT_THANKS)

async def main():
    dp.message_handler(lambda message: True)(collect_data)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
