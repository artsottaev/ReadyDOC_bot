
import openai
import os

openai.api_key = os.getenv("OPENAI_API_KEY")

def ask_question(prompt_text):
    system_prompt = (
        "Ты — российский юрист. Сформулируй ОДИН конкретный вопрос к клиенту, чтобы составить договор "
        "максимально точно и без лишнего. Не задавай больше одного вопроса за раз."
    )
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text}
        ],
        temperature=0.3,
        max_tokens=150
    )
    return response.choices[0].message["content"]

def generate_full_contract(prompt_text):
    system_prompt = (
        "Ты — опытный российский юрист. Составь ПОЛНЫЙ текст договора на основании данных клиента. "
        "Соблюдай актуальное законодательство РФ (2025 год). Включай: преамбулу, предмет, срок, обязанности сторон, "
        "ответственность, оплату, расторжение, подписи. Стиль — официальный. Только текст договора."
    )
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text}
        ],
        temperature=0.2,
        max_tokens=1800
    )
    return response.choices[0].message["content"]

def legal_self_check(doc_text):
    check_prompt = (
        "Проанализируй юридический текст ниже: выяви риски, устаревшие формулировки и нарушения законодательства РФ. "
        "Если всё хорошо — ответь кратко, что документ корректен. Текст:\n\n" + doc_text
    )
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": check_prompt}],
        temperature=0.2,
        max_tokens=600
    )
    return response.choices[0].message["content"]
