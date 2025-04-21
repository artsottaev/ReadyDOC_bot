import os
import json
import logging
import asyncio
import openai
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from docx import Document

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# Google Sheets setup
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds_dict = json.loads(os.getenv('GOOGLE_CREDS_JSON'))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gs = gspread.authorize(creds)
sheet = gs.open('ReadyDoc MVP').sheet1

user_sessions = {}

doc_kb = ReplyKeyboardMarkup(resize_keyboard=True)
doc_kb.add(KeyboardButton("NDA"), KeyboardButton("Акт"), KeyboardButton("Договор"))

def normalize(value):
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    elif isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)

def ask_gpt(prompt_text):
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "Ты помощник по юридическим документам. Отвечай чётко и по делу."},
            {"role": "user", "content": prompt_text}
        ],
        temperature=0.3,
        max_tokens=500
    )
    return response.choices[0].message["content"]

def extract_doc_data(prompt_text):
    system_prompt = (
        "Ты юридический ассистент, помогаешь составить документ по шаблону. "
        "Преобразуй описание в JSON с ключами: тип_документа (services, act, nda), "
        "название_стороны, дата, номер_договора, сумма. Без комментариев и текста, только JSON."
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

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    name = message.from_user.first_name or "друг"
    await message.reply(f"Привет, {name}! Я — ReadyDoc. 🧠\n\nМогу подготовить NDA, акт или договор.\n\n🔹 /getdoc — ручное заполнение\n🔹 /autodoc — автогенерация с помощью ИИ")

@dp.message_handler(commands=['getdoc'])
async def getdoc(message: types.Message):
    user_sessions[message.from_user.id] = {'step': 'choose_doc'}
    await message.reply("Выбери документ, который тебе нужен:", reply_markup=doc_kb)

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get('step') == 'choose_doc')
async def choose_doc(message: types.Message):
    doc_map = {
        "NDA": "nda",
        "Акт": "act",
        "Договор": "services"
    }
    doc_choice = doc_map.get(message.text.strip())
    if not doc_choice:
        return await message.reply("Пожалуйста, выбери документ с кнопок ниже 👇", reply_markup=doc_kb)

    user_sessions[message.from_user.id] = {
        'step': 'collect',
        'doc_type': doc_choice,
        'data': {},
        'fields': ['название_стороны', 'дата', 'номер_договора', 'сумма']
    }
    await message.reply("Отлично! Как называется твоя компания или имя исполнителя?")

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get('step') == 'collect')
async def collect_data(message: types.Message):
    session = user_sessions[message.from_user.id]
    data = session['data']
    fields = session['fields']

    current_field = fields[len(data)]
    data[current_field] = message.text.strip()

    if len(data) < len(fields):
        next_field = fields[len(data)]
        prompts = {
            'дата': "Какая дата в документе?",
            'номер_договора': "Какой номер у договора? (Если нет — напиши 'нет')",
            'сумма': "На какую сумму составлен документ (₽)?"
        }
        await message.reply(prompts.get(next_field, f"Введите значение для {next_field}:"))
    else:
        await message.reply("Генерирую документ...")
        await asyncio.sleep(1.5)
        doc_path = generate_doc(session['doc_type'], data, message.from_user.id)
        row = [str(message.from_user.id), session['doc_type']] + [normalize(v) for v in data.values()] + ['manual']
        sheet.append_row(row)
        await message.reply_document(open(doc_path, 'rb'))
        await message.reply("✅ Готово! Хочешь ещё один документ? Жми /getdoc")
        user_sessions.pop(message.from_user.id)

@dp.message_handler(commands=['autodoc'])
async def autodoc(message: types.Message):
    await message.reply("🧠 Опиши, какой документ тебе нужен.\nНапример:\n\"Договор оказания услуг между ООО и ИП, сумма 150 000, дата 10.05.2025\"")
    user_sessions[message.from_user.id] = {'step': 'awaiting_doc_request'}

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get('step') == 'awaiting_doc_request')
async def handle_doc_request(message: types.Message):
    await message.reply("🤖 Обрабатываю запрос...")
    prompt = message.text.strip()
    data = extract_doc_data(prompt)

    if not data or not all(k in data for k in ['тип_документа', 'название_стороны', 'дата', 'номер_договора', 'сумма']):
        await message.reply("😕 Не смог разобрать данные. Попробуй переформулировать запрос.")
        return

    doc_type = data.pop("тип_документа")
    doc_path = generate_doc(doc_type, data, message.from_user.id)
    row = [str(message.from_user.id), doc_type] + [normalize(v) for v in data.values()] + ['auto']
    sheet.append_row(row)
    await message.reply_document(open(doc_path, 'rb'))
    await message.reply("📄 Готово! Вот твой документ.")
    user_sessions.pop(message.from_user.id)

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
    return output

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)