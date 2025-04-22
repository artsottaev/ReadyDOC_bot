
import openai
import os

openai.api_key = os.getenv("OPENAI_API_KEY")

def generate_full_contract(prompt_text):
    system_prompt = (
        "Ты профессиональный юрист. Составь полный текст официального договора "
        "на основании запроса пользователя. Учитывай: стороны, дату, тип услуг, сумму, срок, "
        "обязанности, ответственность, прочие условия. Пиши строго в юридическом стиле."
        "\n\nОтвет должен быть готовым текстом для вставки в документ — не JSON и не список пунктов."
    )
    completion = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text}
        ],
        temperature=0.4,
        max_tokens=1800
    )
    return completion.choices[0].message["content"]
