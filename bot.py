import os
import json
import logging
from aiogram import Bot, Dispatcher, types, executor
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from markdown2 import markdown
from docx import Document

# Логирование
logging.basicConfig(level=logging.INFO)

# Telegram API
API_TOKEN = os.getenv('API_TOKEN')
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# Google Sheets
scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
creds_dict = json.loads(os.getenv('GOOGLE_CREDS_JSON'))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gs = gspread.authorize(creds)
sheet = gs.open('ReadyDoc MVP').sheet1

# Хранилище сессий
user_sessions = {}

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.reply("Привет! Я ReadyDocBot. Напиши /getdoc, чтобы получить документ.")

@dp.message_handler(commands=['getdoc'])
async def cmd_getdoc(message: types.Message):
    user_sessions[message.from_user.id] = {'step': 'choose_doc'}
    await message.reply("Выберите документ: NDA, Act, Services")

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get('step') == 'choose_doc')
async def process_choice(message: types.Message):
    doc_type = message.text.strip().lower()
    if doc_type not in ('nda', 'act', 'services'):
        return await message.reply("Пожалуйста, выберите: NDA, Act или Services")
    user_sessions[message.from_user.id] = {'step': 'collect', 'doc_type': doc_type, 'data': {}}
    # Задаём первую переменную в зависимости от шаблона
    await message.reply("Введите значение для {{название_стороны}}:")

@dp.message_handler(lambda m: user_sessions.get(m.from_user.id, {}).get('step') == 'collect')
async def process_collect(message: types.Message):
    session = user_sessions[message.from_user.id]
    data = session['data']
    # Определяем, какие поля собирать
    fields = ['название_стороны', 'дата', 'номер_договора', 'сумма']
    idx = len(data)
    key = fields[idx]
    data[key] = message.text.strip()
    if idx + 1 < len(fields):
        await message.reply(f"Введите значение для {{{{{fields[idx+1]}}}}}:")
    else:
        # Всё собрано — записываем в Google Sheets
        sheet.append_row([message.from_user.id, session['doc_type'], *data.values(), 'pending'])
        # Генерируем документ
        path = generate_doc(session['doc_type'], data, message.from_user.id)
        await message.reply_document(open(path, 'rb'))
        user_sessions.pop(message.from_user.id)

def generate_doc(doc_type, data, user_id):
    # Загружаем шаблон
    with open(f'templates/{doc_type}.md', encoding='utf-8') as f:
        text = f.read()

    # Подставляем значения вручную через простой цикл (без .format)
    for key, value in data.items():
        placeholder = f'{{{{{key}}}}}'  # превращает в {{название_стороны}}
        text = text.replace(placeholder, value)

    # Генерируем .docx
    from docx import Document
    doc = Document()
    for line in text.split('\n'):
        doc.add_paragraph(line)
    
    output_path = f'/tmp/{user_id}_{doc_type}.docx'
    doc.save(output_path)
    return output_path

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
