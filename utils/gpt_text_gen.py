import openai

# Настройки для взаимодействия с OpenAI
openai.api_key = 'your_openai_api_key'

# Функция для генерации текста документа
async def gpt_generate_text(user_data):
    # Параметры для запроса в OpenAI
    prompt = f"Генерируй юридический документ для компании {user_data['company_name']}. Юридический адрес: {user_data.get('clarified_info', 'не указан')}. Согласно законодательству РФ на 2025 год."
    
    # Отправка запроса к OpenAI
    response = openai.Completion.create(
        model="gpt-3.5-turbo",
        prompt=prompt,
        max_tokens=1500,
        temperature=0.5
    )
    
    return response['choices'][0]['text'].strip()

# Функция для проверки недостающих данных в документе
async def gpt_check_missing_data(document_text):
    # Пример проверки: если в тексте нет обязательных элементов, возвращаем это
    missing_data = []
    
    if "необходимо" not in document_text:
        missing_data.append("Отсутствует важная фраза 'необходимо'.")
    
    return missing_data

