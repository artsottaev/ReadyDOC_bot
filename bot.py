
import os
import logging
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from utils.gpt_text_gen import ask_for_missing_data, generate_full_contract, legal_self_check
from utils.docgen import generate_doc_from_text
from utils.cache_manager import cache_exists, load_from_cache, save_to_cache

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("API_TOKEN")
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

main_menu = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
main_menu.add(
    KeyboardButton("✍️ Создать документ"),
    KeyboardButton("❌ Отмена")
)

user_sessions = {}

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.reply(
        "Привет! Я ReadyDoc — бот, который генерирует готовые юридические документы. Опиши, что тебе нужно 👇",
        reply_markup=main_menu
    )
    user_sessions[message.from_user.id] = {"step": "awaiting_description"}

@dp.message_handler(lambda m: m.text == "✍️ Создать документ")
async def create_document(message: types.Message):
    await message.reply("📝 Опиши, какой договор документ:»)
    user_sessions[message.from_user.id] = {"step": "awaiting_description"}

@dp.message_handler(lambda m: m.text == "❌ Отмена")
async def cancel(message: types.Message):
    user_sessions.pop(message.from_user.id, None)
    await message.reply("Ок! Если нужно — нажми «Создать документ»", reply_markup=main_menu)

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get("step") == "awaiting_description")
async def handle_description(message: types.Message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    user_sessions[user_id]["step"] = "processing"

    await message.reply("🔍 Проверяю, можно ли составить документ…»)

    if cache_exists(prompt):
        text = load_from_cache(prompt)
        await message.reply("📦 Нашёл похожий запрос")
    else:
        followup = ask_for_missing_data(prompt)
        if "?" in followup:
            await message.reply(f"🤔 Пожалуйста, уточни:
{followup}")
            user_sessions[user_id] = {"step": "awaiting_clarification", "original_prompt": prompt}
            return
        else:
            text = generate_full_contract(prompt)
            save_to_cache(prompt, text)

    doc_path = generate_doc_from_text(text, user_id)
    await message.reply_document(open(doc_path, "rb"), caption="📄 Документ готов.")

    check_result = legal_self_check(text)
    await message.reply(f"⚖️ Юридическая проверка:
{check_result}")

    user_sessions.pop(user_id, None)

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get("step") == "awaiting_clarification")
async def handle_clarification(message: types.Message):
    user_id = message.from_user.id
    original = user_sessions[user_id].get("original_prompt", "")
    combined_prompt = f"{original}. Дополнение: {message.text.strip()}"

    await message.reply("🔄 Обрабатываю дополненную информацию...")

    text = generate_full_contract(combined_prompt)
    save_to_cache(combined_prompt, text)

    doc_path = generate_doc_from_text(text, user_id)
    await message.reply_document(open(doc_path, "rb"), caption="📄 Документ готов.")

    check_result = legal_self_check(text)
    await message.reply(f"⚖️ Юридическая проверка:
{check_result}")

    except Exception as e:
        logging.error(f"Ошибка генерации: {e}")
        await message.reply("⚠️ Что-то пошло не так. Попробуй снова или измени описание.")

    user_sessions.pop(user_id, None)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
