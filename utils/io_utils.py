import os
import json
import base64
from config.constants import CACHE_DIR, CACHE_FILE

def setup_directories(run_path):
    os.makedirs(run_path, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_cache(cache_data):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, indent=4)

def get_image_as_base64(image_path):
    try:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except IOError as e:
        print(f"Error leyendo la imagen {image_path}: {e}")
        return None
