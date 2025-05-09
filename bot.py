import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from dotenv import load_dotenv
import openai

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv('BOT_TOKEN'), parse_mode=ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

openai.api_key = os.getenv('OPENAI_API_KEY')

class DocumentCreation(StatesGroup):
    waiting_for_missing_info = State()
    waiting_for_post_edit = State()

def parse_intent(text: str) -> dict:
    result = {}
    lowered = text.lower()

    if "аренда" in lowered:
        result["document_type"] = "договор аренды"
        result["purpose"] = "аренда имущества"
    elif "поставк" in lowered:
        result["document_type"] = "договор поставки"
        result["purpose"] = "поставка товаров"
    elif "оказание услуг" in lowered or "услуги" in lowered:
        result["document_type"] = "договор оказания услуг"
        result["purpose"] = "оказание услуг"
    elif "подряд" in lowered:
        result["document_type"] = "договор подряда"
        result["purpose"] = "выполнение работ по заказу"

    if any(entity in lowered for entity in ["ип", "ооо", "заказчик", "исполнитель"]):
        result["parties"] = text  # упрощённо считаем, что стороны указаны в тексте

    return result

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await message.answer("Здравствуйте! Опишите, какой документ вам нужен (например: договор аренды между ИП и ООО).")
    await state.set_state(DocumentCreation.waiting_for_missing_info)

@router.message(DocumentCreation.waiting_for_missing_info)
async def handle_initial_description(message: Message, state: FSMContext):
    parsed = parse_intent(message.text)
    await state.update_data(**parsed)
    data = await state.get_data()

    missing = []
    if "document_type" not in data:
        missing.append("тип документа")
    if "parties" not in data:
        missing.append("стороны")
    if "purpose" not in data:
        missing.append("цель")

    if missing:
        await message.answer(f"Пожалуйста, уточните: {', '.join(missing)}")
        return

    await message.answer("Формирую черновик документа, пожалуйста, подождите...")
    doc_text = await generate_document(data)
    await state.update_data(generated_doc=doc_text)
    await message.answer(doc_text)

    await message.answer("Хотите ли вы добавить особые условия или уточнения к этому документу?")
    await state.set_state(DocumentCreation.waiting_for_post_edit)

@router.message(DocumentCreation.waiting_for_post_edit)
async def handle_post_edit(message: Message, state: FSMContext):
    user_note = message.text
    data = await state.get_data()
    prompt = (
        f"Допиши/уточни следующий юридический документ с учётом следующих требований пользователя:\n"
        f"\nДокумент:\n{data['generated_doc']}\n"
        f"\nТребования пользователя:\n{user_note}"
    )
    await message.answer("Уточняю документ...")
    try:
        refined = await generate_document({"prompt_override": prompt})
        await message.answer(refined)
    except Exception as e:
        logging.error(f"Ошибка уточнения документа: {e}")
        await message.answer("Произошла ошибка при уточнении документа.")
    await state.clear()

async def generate_document(data: dict) -> str:
    if "prompt_override" in data:
        prompt = data["prompt_override"]
    else:
        prompt = (
            f"Создай юридический документ на русском языке, соответствующий законодательству РФ.\n\n"
            f"Тип документа: {data['document_type']}\n"
            f"Стороны: {data['parties']}\n"
            f"Цель документа: {data['purpose']}\n\n"
            f"Документ должен быть официальным, юридически грамотным и готовым для использования."
        )

    completion = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Ты опытный юрист, создающий юридические документы."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=3000
    )
    return completion.choices[0].message.content.strip()

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
