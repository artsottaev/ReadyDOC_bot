import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

from utils.settings import BOT_TOKEN
from utils.prompts import *
from utils.gpt_text_gen import gpt_generate_text, gpt_check_missing_data
from utils.legal_checker import check_document_legality
from utils.docgen import generate_docx

# Загружаем .env переменные
load_dotenv()

logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Тексты сообщений
TEXT_WELCOME = "👋 Привет! Я ReadyDoc, твой помощник в создании юридических документов.\n\nНачнём собирать данные 📑. Просто отвечай на мои вопросы."
TEXT_COLLECTING = "✏️ Пожалуйста, напиши название своей компании (или своё ФИО для частного договора)."
TEXT_CLARIFYING = "🔍 Теперь уточним детали.\n\nНапример: какая цель документа? Какие важные условия нужно включить?"
TEXT_GENERATING = "⚙️ Генерирую проект документа... Пожалуйста, подожди пару секунд."
TEXT_CHECKING_LEGALITY = "🧐 Проверяю соответствие документа действующему законодательству РФ..."
TEXT_LEGAL_ISSUES = "⚠️ Найдены потенциальные юридические проблемы:\n\n{issues}\n\nХотите, я попробую их исправить?"
TEXT_FIX_ISSUES = "🔧 Исправляю выявленные проблемы..."
TEXT_DOCUMENT_OK = "✅ Документ соответствует требованиям законодательства. Готов отправить!"
TEXT_DOCUMENT_READY = "📄 Вот ваш документ! Можете его скачать, проверить и использовать."
TEXT_THANKS = "🙏 Спасибо, что воспользовались ReadyDoc! Если понадобится помощь — просто напишите."
TEXT_ERROR = "🚫 Что-то пошло не так. Попробуйте ещё раз позже или обратитесь в поддержку."

# Состояния FSM
class ReadyDocFSM:
    collecting_data = "collecting_data"
    clarifying_data = "clarifying_data"
    generating_draft = "generating_draft"
    legal_check = "legal_check"
    finalizing_document = "finalizing_document"
    sending_result = "sending_result"

# Функция для отображения "typing..." эффекта
async def send_typing_effect(message: Message, text: str):
    await bot.send_chat_action(message.chat.id, 'typing')
    await asyncio.sleep(2)  # эмулируем задержку набора текста
    await message.answer(text)

# Кнопки
def create_inline_buttons():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton("✅ Всё верно", callback_data="confirm")],
            [InlineKeyboardButton("🔧 Исправить", callback_data="fix")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ]
    )

# Шаг 1: Сбор данных
async def collect_data(message: Message):
    await send_typing_effect(message, TEXT_WELCOME)
    await message.answer(TEXT_COLLECTING)
    user_data = await dp.storage.get_data(chat_id=message.chat.id, user_id=message.from_user.id)

    # Логика для сбора данных
    user_data["company_name"] = message.text  # Пример
    await dp.storage.set_data(chat_id=message.chat.id, user_id=message.from_user.id, data=user_data)

    await message.answer(TEXT_CLARIFYING, reply_markup=create_inline_buttons())

# Шаг 2: Уточнение данных
async def collect_clarification(message: Message):
    user_data = await dp.storage.get_data(chat_id=message.chat.id, user_id=message.from_user.id)

    clarification_question = TEXT_CLARIFYING
    await message.answer(clarification_question)

    if message.text:  # Если есть ответ
        user_data["clarified_info"] = message.text

    await dp.storage.set_data(chat_id=message.chat.id, user_id=message.from_user.id, data=user_data)

    await message.answer(TEXT_GENERATING, reply_markup=create_inline_buttons())
    await generate_draft(message)

# Шаг 3: Генерация документа
async def generate_draft(message: Message):
    user_data = await dp.storage.get_data(chat_id=message.chat.id, user_id=message.from_user.id)

    try:
        document_text = await gpt_generate_text(user_data)
    except Exception:
        await message.answer(TEXT_ERROR)
        return

    await send_typing_effect(message, TEXT_CHECKING_LEGALITY)
    await legal_check(message, document_text)

# Шаг 4: Юридическая проверка
async def legal_check(message: Message, document_text: str):
    try:
        legal_issues = await check_document_legality(document_text)
    except Exception:
        await message.answer(TEXT_ERROR)
        return

    if legal_issues:
        await message.answer(TEXT_LEGAL_ISSUES.format(issues=legal_issues))
        await message.answer(TEXT_FIX_ISSUES)
        await fix_issues(message)
    else:
        await message.answer(TEXT_DOCUMENT_OK)
        await finalize_document(message, document_text)

# Шаг 5: Исправление проблем
async def fix_issues(message: Message):
    await message.answer(TEXT_FIXING)
    await generate_draft(message)  # Повторная генерация с исправлениями

# Шаг 6: Финализация и отправка документа
async def finalize_document(message: Message, document_text: str):
    try:
        doc_file = await generate_docx(document_text)
    except Exception:
        await message.answer(TEXT_ERROR)
        return

    await message.answer(TEXT_DOCUMENT_READY)
    await message.answer_document(doc_file)

    await message.answer(TEXT_THANKS)

# Обработчик Inline кнопок
@dp.callback_query_handler(lambda c: c.data in ["confirm", "fix", "cancel"])
async def process_callback(callback_query):
    action = callback_query.data
    message = callback_query.message

    if action == "confirm":
        await message.answer("✅ Всё верно! Документ готов.")
        await finalize_document(message, "Текст документа (заменить на реальный)")
    elif action == "fix":
        await message.answer("🔧 Начинаю исправления...")
        await fix_issues(message)
    elif action == "cancel":
        await message.answer("❌ Операция отменена.")
        await message.answer(TEXT_THANKS)

async def main():
    # Регистрация хендлеров
    dp.message.register(collect_data, F.text)

    # Старт
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())