import os
import logging
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from docx import Document
from utils.gpt_text_gen import generate_full_contract
from utils.docgen import generate_doc, normalize
from utils.gpt import extract_doc_data, gpt_add_section
from utils.sheets import save_row

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("API_TOKEN")
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# Главное меню
main_menu = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
main_menu.add(
    KeyboardButton("✍️ Создать документ"),
    KeyboardButton("❌ Отмена")
)

user_sessions = {}

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.reply(
        "Привет! Я помогу тебе составить юридический договор. Просто опиши, что нужно 👇\n\n"
        "Например: \"договор на оказание услуг между ООО и ИП на 120 тыс с 1 мая\"",
        reply_markup=main_menu
    )
    user_sessions[message.from_user.id] = {"step": "awaiting_description"}

@dp.message_handler(lambda m: m.text == "✍️ Создать документ")
async def manual_start(message: types.Message):
    await message.reply("📝 Опиши, какой договор нужен:")
    user_sessions[message.from_user.id] = {"step": "awaiting_description"}

@dp.message_handler(lambda m: m.text == "❌ Отмена")
async def cancel(message: types.Message):
    user_sessions.pop(message.from_user.id, None)
    await message.reply("Окей! Чтобы начать снова — нажми «Создать документ»", reply_markup=main_menu)

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get("step") == "awaiting_description")
async def handle_description(message: types.Message):
    prompt = message.text.strip()
    await message.reply("🤖 Генерирую документ... Это может занять 5–10 секунд.")

    try:
        contract_text = generate_full_contract(prompt)

        # Сохраняем текст в Word-файл
        doc = Document()
        for line in contract_text.split("\n"):
            doc.add_paragraph(line)

        file_path = f"/tmp/contract_{message.from_user.id}.docx"
        doc.save(file_path)

        await message.reply_document(open(file_path, "rb"), caption="✅ Готово! Вот твой договор.")
    except Exception as e:
        logging.error(f"Ошибка генерации: {e}")
        await message.reply("⚠️ Что-то пошло не так. Попробуй снова или измени описание.")

    user_sessions.pop(message.from_user.id, None)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)