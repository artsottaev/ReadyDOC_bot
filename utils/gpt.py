
import openai
import json
import os

openai.api_key = os.getenv("OPENAI_API_KEY")

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
    return original_text + "\n\n" + completion.choices[0].message["content"]
