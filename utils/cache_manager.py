
import json
import hashlib
from pathlib import Path

CACHE_DIR = Path("cache")

def get_cache_path(key):
    h = hashlib.sha256(key.encode()).hexdigest()
    return CACHE_DIR / f"{h}.json"

def cache_exists(key):
    return get_cache_path(key).exists()

def save_to_cache(key, value):
    path = get_cache_path(key)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({"data": value}, f)

def load_from_cache(key):
    path = get_cache_path(key)
    if not path.exists():
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f).get("data")
