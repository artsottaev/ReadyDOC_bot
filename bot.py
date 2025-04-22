import os
import json
import logging
import asyncio
import openai
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from docx import Document
from utils.docgen import generate_doc, normalize
from utils.gpt import extract_doc_data, gpt_add_section
from utils.sheets import save_row

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_dict = json.loads(os.getenv('GOOGLE_CREDS_JSON'))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gs = gspread.authorize(creds)
sheet = gs.open('ReadyDoc MVP').sheet1

user_sessions = {}

main_menu = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
main_menu.add(
    KeyboardButton("✍️ Создать документ"),
    KeyboardButton("⚙️ Настроить документ"),
    KeyboardButton("🔁 Доработать"),
    KeyboardButton("📄 Мои документы")
)

def normalize(value):
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    elif isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)

def extract_doc_data(prompt_text):
    system_prompt = (
        "Ты юридический ассистент. Преобразуй описание в JSON с ключами: "
        "тип_документа (services, act, nda), название_стороны, дата, номер_договора, сумма. "
        "Без комментариев и лишнего текста. Только JSON."
    )
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text}
        ],
        temperature=0,
        max_tokens=500
    )
    reply = completion.choices[0].message["content"]
    try:
        return json.loads(reply)
    except json.JSONDecodeError:
        return None

def gpt_add_section(original_text, section_topic):
    prompt = f"Добавь в конец этого договора пункт о {section_topic} в официальном юридическом стиле. Документ:\n\n{original_text}"
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты юрист. Пиши только текст для вставки в договор."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=300
    )
    addition = completion.choices[0].message["content"]
    return original_text + "\n\n" + addition

def generate_doc(doc_type, data, user_id):
    with open(f'templates/{doc_type}.md', encoding='utf-8') as f:
        text = f.read()
    for key, value in data.items():
        text = text.replace(f'{{{{{key}}}}}', normalize(value))
    doc = Document()
    for line in text.split('\n'):
        doc.add_paragraph(line)
    output = f'/tmp/{user_id}_{doc_type}.docx'
    doc.save(output)
    return output, text

@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.reply("Привет! Я ReadyDoc. Выбери, что хочешь сделать 👇", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "✍️ Создать документ")
async def create_doc(message: types.Message):
    await message.reply("🧠 Опиши, какой документ тебе нужен.\nНапример: \"Договор оказания услуг между ООО и ИП, сумма 150 000, дата 10.05.2025\"")
    user_sessions[message.from_user.id] = {'step': 'awaiting_doc_request'}

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get('step') == 'awaiting_doc_request')
async def handle_doc_request(message: types.Message):
    prompt = message.text.strip()
    await message.reply("🤖 Обрабатываю...")
    data = extract_doc_data(prompt)

    if not data or not all(k in data for k in ['тип_документа', 'название_стороны', 'дата', 'номер_договора', 'сумма']):
        return await message.reply("😕 Не смог разобрать данные. Попробуй переформулировать.")

    doc_type = data.pop("тип_документа")
    path, raw_text = generate_doc(doc_type, data, message.from_user.id)

    user_sessions[message.from_user.id] = {
        'last_doc_type': doc_type,
        'last_data': data,
        'last_text': raw_text
    }

    row = [str(message.from_user.id), doc_type] + [normalize(v) for v in data.values()] + ['auto']
    sheet.append_row(row)

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Добавить пункт", callback_data="add_section"),
        InlineKeyboardButton("✅ Завершить", callback_data="done")
    )

    await message.reply_document(open(path, 'rb'), caption="📄 Документ готов. Что дальше?", reply_markup=markup)

@dp.callback_query_handler(lambda c: c.data == "add_section")
async def handle_addition_request(call: types.CallbackQuery):
    await call.message.edit_reply_markup()
    await call.message.answer("Что ты хочешь добавить? Например: ответственность, конфиденциальность, штрафы")
    user_sessions[call.from_user.id]['step'] = 'awaiting_addition'

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get('step') == 'awaiting_addition')
async def handle_addition_input(message: types.Message):
    section = message.text.strip()
    session = user_sessions[message.from_user.id]
    base_text = session['last_text']
    new_text = gpt_add_section(base_text, section)

    session['last_text'] = new_text

    doc = Document()
    for line in new_text.split('\n'):
        doc.add_paragraph(line)
    output = f'/tmp/{message.from_user.id}_final.docx'
    doc.save(output)

    await message.reply_document(open(output, 'rb'), caption="📄 Готово! Пункт добавлен.")
    await message.reply("Хочешь что-то ещё изменить или закончить?", reply_markup=InlineKeyboardMarkup().add(
        InlineKeyboardButton("➕ Добавить ещё", callback_data="add_section"),
        InlineKeyboardButton("✅ Завершить", callback_data="done")
    ))

@dp.callback_query_handler(lambda c: c.data == "done")
async def handle_done(call: types.CallbackQuery):
    await call.message.edit_reply_markup()
    await call.message.answer("✅ Готово. Документ сохранён. Чтобы начать заново — нажми «Создать документ»", reply_markup=main_menu)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)