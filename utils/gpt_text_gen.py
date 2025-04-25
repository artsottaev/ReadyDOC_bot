import openai
import os

openai.api_key = os.getenv("OPENAI_API_KEY")

def extract_followup_questions(prompt_text):
    instruction = (
        "Ты — профессиональный юрист. Клиент дал неполное описание для составления договора:\n"
        f"{prompt_text}\n\n"
        "Сформулируй список из 3–5 уточняющих вопросов. Кратко, по одному на строку."
    )
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": instruction}],
        temperature=0.3,
        max_tokens=500
    )
    questions = completion.choices[0].message["content"]
    return [q.strip("-•1234567890. ") for q in questions.strip().splitlines() if q.strip()]

def generate_full_contract(prompt_text):
    system_prompt = (
        "Ты — опытный российский юрист. Составь ПОЛНЫЙ текст договора на основании описания клиента. "
        "Учитывай законодательство РФ (актуально на 2025 год). Включай:\n"
        "- Преамбулу\n- Предмет договора\n- Сроки\n- Обязанности сторон\n- Ответственность\n"
        "- Оплата\n- Расторжение\n- Реквизиты\n\n"
        "Ответ — только юридический текст, готовый для печати."
    )
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text}
        ],
        temperature=0.3,
        max_tokens=1800
    )
    return completion.choices[0].message["content"]

def legal_self_check_and_extend(doc_text):
    check_and_extend_prompt = (
        "Ты — опытный юрист. Прочитай договор ниже, оцени соответствие законодательству РФ и ДОПОЛНИ его, "
        "если чего-то не хватает: обязательные условия, ссылки на статьи, стандартные формулировки.\n\n"
        "Текст:\n" + doc_text
    )
    result = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": check_and_extend_prompt}],
        temperature=0.2,
        max_tokens=1800
    )
    return result.choices[0].message["content"]
