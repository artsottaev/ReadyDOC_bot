import os
import json
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from docx import Document

logging.basicConfig(level=logging.INFO)

API_TOKEN = os.getenv("API_TOKEN")
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# Google Sheets setup
scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
creds_dict = json.loads(os.getenv('GOOGLE_CREDS_JSON'))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gs = gspread.authorize(creds)
sheet = gs.open('ReadyDoc MVP').sheet1

# Session store
user_sessions = {}

# Custom keyboard
doc_kb = ReplyKeyboardMarkup(resize_keyboard=True)
doc_kb.add(KeyboardButton("NDA"), KeyboardButton("Акт"), KeyboardButton("Договор"))

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    name = message.from_user.first_name or "друг"
    await message.reply(f"Привет, {name}! Я — ReadyDoc.\n\nМогу подготовить NDA, акт или договор.\nНажми /getdoc, чтобы начать!")

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
    doc_choice = doc_map.get(message.text.strip().title())
    if not doc_choice:
        return await message.reply("Пожалуйста, выбери документ с кнопок ниже 👇", reply_markup=doc_kb)

    user_sessions[message.from_user.id] = {
        'step': 'collect',
        'doc_type': doc_choice,
        'data': {},
        'fields': [
            'название_заказчика',
            'название_исполнителя',
            'дата',
            'номер_договора',
            'сумма'
        ]
    }
    await message.reply("Отлично! Начнём.\nКак называется компания или ФИО заказчика?")

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
            'название_исполнителя': "Как называется компания или ФИО исполнителя?",
            'дата': "Какая дата в документе?",
            'номер_договора': "Какой номер у договора? (Если нет — напиши 'нет')",
            'сумма': "На какую сумму составлен документ (₽)?"
        }
        await message.reply(prompts.get(next_field, f"Введите значение для {next_field}:"))
    else:
        await message.reply("Генерирую документ...")
        await asyncio.sleep(1.5)
        doc_path = generate_doc(session['doc_type'], data, message.from_user.id)
        sheet.append_row([message.from_user.id, session['doc_type'], *data.values(), 'done'])
        await message.reply_document(open(doc_path, 'rb'))
        await message.reply("Вот твой файл. Хочешь сделать ещё один? Просто нажми /getdoc")
        user_sessions.pop(message.from_user.id, None)

def generate_doc(doc_type, data, user_id):
    with open(f'templates/{doc_type}.md', encoding='utf-8') as f:
        text = f.read()
    for key, value in data.items():
        text = text.replace(f'{{{{{key}}}}}', value)
    doc = Document()
    for line in text.split('\n'):
        doc.add_paragraph(line)
    output = f'/tmp/{user_id}_{doc_type}.docx'
    doc.save(output)
    return output

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)