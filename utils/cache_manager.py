
import hashlib
import json
from pathlib import Path

CACHE_DIR = Path("cache")

def normalize_query(query):
    return query.strip().lower()

def get_cache_path(prompt):
    h = hashlib.sha256(prompt.encode()).hexdigest()
    return CACHE_DIR / f"{h}.json"

def cache_exists(prompt):
    return get_cache_path(prompt).exists()

def save_to_cache(prompt, text):
    path = get_cache_path(prompt)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({"prompt": prompt, "text": text}, f)

def load_from_cache(prompt):
    path = get_cache_path(prompt)
    if not path.exists():
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f).get("text")
