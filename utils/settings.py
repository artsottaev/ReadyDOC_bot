import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❗ BOT_TOKEN не найден. Убедись, что он указан в .env или в переменных окружения.")

