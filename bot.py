
import os
import logging
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from utils.gpt_text_gen import extract_followup_questions, generate_full_contract, legal_self_check_and_extend
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
    await message.reply("Привет! Я ReadyDoc. Опиши, какой договор тебе нужен.", reply_markup=main_menu)
    user_sessions[message.from_user.id] = {"step": "awaiting_description"}

@dp.message_handler(lambda m: m.text == "✍️ Создать документ")
async def create_document(message: types.Message):
    await message.reply("📝 Опиши, какой документ нужен:")
    user_sessions[message.from_user.id] = {"step": "awaiting_description"}

@dp.message_handler(lambda m: m.text == "❌ Отмена")
async def cancel(message: types.Message):
    user_sessions.pop(message.from_user.id, None)
    await message.reply("Ок! Если нужно — нажми «Создать документ»", reply_markup=main_menu)

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get("step") == "awaiting_description")
async def handle_description(message: types.Message):
    user_id = message.from_user.id
    prompt = message.text.strip()
    user_sessions[user_id] = {
        "step": "asking_followups",
        "prompt": prompt,
        "answers": [],
        "questions": extract_followup_questions(prompt)
    }
    await ask_next_question(message, user_id)

async def ask_next_question(message, user_id):
    session = user_sessions[user_id]
    questions = session["questions"]
    answers = session["answers"]
    if len(answers) < len(questions):
        await message.reply(f"❓ {questions[len(answers)]}")
    else:
        full_prompt = (
    "Составь полный юридический договор, соответствующий законодательству РФ на 2025 год."
    "Используй следующую информацию от клиента:\n\n"
    f"Описание: {session['prompt']}\n"
    f"Дополнительно:\n" + "\n".join(session["answers"])
)       
	await message.reply("📄 Генерирую договор на основе твоих ответов...")
        text = generate_full_contract(full_prompt)
        text = legal_self_check_and_extend(text)
        save_to_cache(full_prompt, text)
        doc_path = generate_doc_from_text(text, user_id)
        await message.reply_document(open(doc_path, "rb"), caption="✅ Готово! Документ составлен.")
        user_sessions.pop(user_id, None)

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get("step") == "asking_followups")
async def handle_followup_answer(message: types.Message):
    user_id = message.from_user.id
    session = user_sessions[user_id]
    session["answers"].append(message.text.strip())
    await ask_next_question(message, user_id)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
