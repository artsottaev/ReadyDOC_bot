import openai
import os

openai.api_key = os.getenv("OPENAI_API_KEY")

def generate_full_contract(prompt_text):
    system_prompt = (
        "Ты — опытный российский юрист. Составь ПОЛНЫЙ, ГОТОВЫЙ К ПЕЧАТИ текст договора "
        "на основании описания клиента. Учитывай нормы действующего законодательства РФ (ГК РФ, ТК РФ, НК РФ), "
        "актуальные на 2025 год.\n\n"
        "Структура договора должна включать:\n"
        "1. Преамбулу (место, дата, стороны)\n"
        "2. Предмет договора\n"
        "3. Сроки исполнения\n"
        "4. Обязанности сторон\n"
        "5. Ответственность сторон\n"
        "6. Условия оплаты\n"
        "7. Порядок расторжения\n"
        "8. Заключительные положения\n"
        "9. Реквизиты и подписи сторон\n\n"
        "Пиши строго в официальном юридическом стиле. Ответ — только текст договора, без пояснений."
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

def ask_for_missing_data(prompt_text):
    clarification_prompt = (
        "Ты юрист. Получен неполный запрос клиента:\n"
        f"{prompt_text}\n\n"
        "Какие 2-3 вопроса ты бы задал, чтобы можно было составить корректный юридический договор?"
    )
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": clarification_prompt}],
        temperature=0.3,
        max_tokens=300
    )
    return completion.choices[0].message["content"]

def legal_self_check(doc_text):
    check_prompt = (
        "Проверь текст договора ниже на соответствие действующему законодательству РФ. "
        "Отметь устаревшие формулировки, недостающие обязательные элементы и потенциальные риски:\n\n"
        f"{doc_text}"
    )
    result = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": check_prompt}],
        temperature=0.2,
        max_tokens=500
    )
    return result.choices[0].message["content"]
