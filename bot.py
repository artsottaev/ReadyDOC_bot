import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.enums.parse_mode import ParseMode
from aiogram.types import Message, FSInputFile
from openai import AsyncOpenAI
from dotenv import load_dotenv
from docx import Document

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# Валидация
if not BOT_TOKEN or not OPENAI_API_KEY:
    raise ValueError("BOT_TOKEN и OPENAI_API_KEY обязательны!")

# Логгирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
storage = RedisStorage.from_url(f"redis://{REDIS_HOST}:{REDIS_PORT}/0")
dp = Dispatcher(storage=storage)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# FSM
class DocGenState(StatesGroup):
    waiting_for_initial_input = State()
    waiting_for_special_terms = State()


# GPT генерация
async def generate_gpt_response(system_prompt: str, user_prompt: str) -> str:
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=3000
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Ошибка OpenAI: {e}")
        return "❌ Произошла ошибка при генерации документа. Попробуйте позже."


# Сохранение в .docx
def save_docx(text: str, filename: str) -> str:
    doc = Document()
    for para in text.split("\n"):
        doc.add_paragraph(para)
    filepath = os.path.join("/tmp", filename)
    doc.save(filepath)
    return filepath


# /start
@dp.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я помогу составить юридический документ.\n\n"
        "Просто опиши, какой документ тебе нужен. Например:\n"
        "<i>Нужен договор аренды офиса между ИП и ООО на год</i>"
    )
    await state.set_state(DocGenState.waiting_for_initial_input)


# Обработка описания
@dp.message(DocGenState.waiting_for_initial_input)
async def handle_description(message: Message, state: FSMContext):
    if len(message.text) > 3000:
        await message.answer("⚠️ Слишком длинный текст. Укороти, пожалуйста.")
        return

    await state.update_data(initial_text=message.text)
    prompt = f"Составь юридический документ по российскому праву. Вот описание от пользователя:\n\n\"{message.text}\""
    await message.answer("🧠 Генерирую черновик документа...")

    document = await generate_gpt_response(
        "Ты опытный юрист. Составь юридически корректный документ.",
        prompt
    )

    await state.update_data(document_text=document)
    filename = f"draft_{message.from_user.id}.docx"
    path = save_docx(document, filename)
    await message.answer("📄 Вот сгенерированный документ:")
    await message.answer_document(FSInputFile(path))
    await message.answer("Хочешь добавить особые условия? Напиши их или напиши <b>нет</b>.")
    await state.set_state(DocGenState.waiting_for_special_terms)


# Обработка уточнений
@dp.message(DocGenState.waiting_for_special_terms)
async def handle_additions(message: Message, state: FSMContext):
    data = await state.get_data()
    base_text = data.get("document_text", "")

    if message.text.strip().lower() == "нет":
        await message.answer("✅ Документ завершён. Удачи!")
        await state.clear()
        return

    prompt = (
        "Вот документ. Добавь в него аккуратно следующие особые условия, сохранив стиль и структуру:\n\n"
        f"Условия: {message.text}\n\nДокумент:\n{base_text}"
    )
    await message.answer("🔧 Вношу изменения...")

    updated_doc = await generate_gpt_response(
        "Ты юридический редактор. Вноси только необходимые правки, сохраняя стиль.",
        prompt
    )

    final_path = save_docx(updated_doc, f"final_{message.from_user.id}.docx")
    await message.answer("📄 Документ с учётом условий:")
    await message.answer_document(FSInputFile(final_path))
    await message.answer("✅ Готово!")
    await state.clear()


# Запуск
if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
