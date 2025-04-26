import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties

from utils.gpt_text_gen import ask_question, generate_full_contract, legal_self_check
from utils.docgen import generate_doc_from_text

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("API_TOKEN")

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📄 Новый документ")]
    ],
    resize_keyboard=True
)

user_data = {}

@dp.message(F.text == "/start")
async def start(message: Message):
    user_data[message.from_user.id] = {"step": "awaiting_type", "context": ""}
    await message.answer("Привет! Какой документ тебе нужен?", reply_markup=main_menu)

@dp.message(F.text == "📄 Новый документ")
async def new_doc(message: Message):
    user_data[message.from_user.id] = {"step": "awaiting_type", "context": ""}
    await message.answer("📝 Какой документ нужен? Например: договор подряда, NDA, аренды…")

@dp.message(F.text)
async def handle_input(message: Message):
    uid = message.from_user.id
    if uid not in user_data:
        user_data[uid] = {"step": "awaiting_type", "context": ""}

    session = user_data[uid]
    step = session["step"]
    context = session["context"]

    if step == "awaiting_type":
        session["context"] = message.text.strip()
        session["step"] = "clarifying"
        question = ask_question(session["context"])
        await message.answer(f"❓ {question}")
        return

    elif step == "clarifying":
        session["context"] += f"\nДополнение: {message.text.strip()}"
        question = ask_question(session["context"])
        if "?" in question:
            await message.answer(f"❓ {question}")
        else:
            session["step"] = "generating"
            await message.answer("📄 Генерирую договор…")
            text = generate_full_contract(session["context"])
            session["contract"] = text
            await message.answer("🔎 Проверяю соответствие закону…")
            review = legal_self_check(text)
            session["review"] = review
            if "корректен" in review.lower():
                session["step"] = "ready"
                path = generate_doc_from_text(text, uid)
                await message.answer_document(open(path, "rb"), caption="✅ Готово! Документ соответствует закону.")
            else:
                session["step"] = "ask_fix"
                await message.answer(f"⚠️ Найдены риски:\n{review}\n\nХотите, чтобы я исправил документ? (да/нет)")

    elif step == "ask_fix":
        if message.text.lower() in ["да", "исправь", "давай"]:
            session["context"] += "\nПросьба: исправь риски и приведи в соответствие с законом"
            fixed = generate_full_contract(session["context"])
            path = generate_doc_from_text(fixed, uid)
            await message.answer_document(open(path, "rb"), caption="✅ Документ исправлен и готов.")
        else:
            path = generate_doc_from_text(session["contract"], uid)
            await message.answer_document(open(path, "rb"), caption="📎 Документ с замечаниями.")
        user_data.pop(uid)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())