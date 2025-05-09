import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.enums.parse_mode import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

class DocGenState(StatesGroup):
    waiting_for_initial_input = State()
    waiting_for_special_terms = State()

@dp.message(F.text)
async def handle_initial_description(message: Message, state: FSMContext):
    await state.update_data(initial_text=message.text)

    # Подставляем то, что пользователь уже написал как суть документа
    prompt = f"Составь юридический документ: {message.text}"
    await message.answer("Генерирую документ... 🧠")

    response = await openai_client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Ты опытный юрист, создающий юридические документы по российскому праву."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=3000
    )

    document_text = response.choices[0].message.content.strip()
    await state.update_data(document_text=document_text)

    await message.answer("Вот сгенерированный документ:\n\n" + document_text)
    await message.answer("Есть ли особые условия, которые нужно добавить в документ? Напиши их, если нужно. Или напиши 'нет'.")
    await state.set_state(DocGenState.waiting_for_special_terms)

@dp.message(DocGenState.waiting_for_special_terms)
async def handle_special_terms(message: Message, state: FSMContext):
    data = await state.get_data()
    base_text = data["document_text"]

    if message.text.strip().lower() == "нет":
        await message.answer("Документ завершён ✅")
        await state.clear()
        return

    # Добавим условия как post-processing через GPT
    post_prompt = (
        "Вот документ, созданный юристом. Добавь в него следующие особые условия: "
        f"{message.text}\n\nДокумент:\n{base_text}"
    )

    response = await openai_client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Ты юридический редактор. Добавь условия корректно, соблюдая стиль документа."},
            {"role": "user", "content": post_prompt}
        ],
        temperature=0.3,
        max_tokens=3000
    )

    final_doc = response.choices[0].message.content.strip()
    await message.answer("Документ с особыми условиями:\n\n" + final_doc)
    await message.answer("✅ Готово")
    await state.clear()

if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
