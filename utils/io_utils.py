"""
Utilidades para operaciones de entrada/salida y logging.

Este módulo proporciona funciones para manejar directorios, caché,
conversión de imágenes a base64 y logging de llamadas a OpenAI.
"""

import base64
import json
import os
from datetime import datetime

from config.constants import CACHE_DIR, CACHE_FILE

# Variable global para almacenar logs de OpenAI
_openai_logs = []

def setup_directories(run_path):
    os.makedirs(run_path, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

def log_openai_call(prompt, response, model="gpt-4o", call_type="text"):
    """
    Registra una llamada a OpenAI para inspección posterior
    
    Args:
        prompt: El prompt enviado a OpenAI
        response: La respuesta recibida de OpenAI
        model: El modelo usado (por defecto 'gpt-4o')
        call_type: Tipo de llamada ('text', 'vision', etc.)
    """
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "type": call_type,
        "model": model,
        "prompt": prompt,
        "response": response
    }
    _openai_logs.append(log_entry)

def save_openai_logs(run_path):
    """
    Guarda los logs de OpenAI en un archivo JSON
    
    Args:
        run_path: Directorio donde guardar el archivo de logs
    """
    if _openai_logs:
        log_file = os.path.join(run_path, "openai_logs.json")
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(_openai_logs, f, indent=2, ensure_ascii=False)
        print(f"📝 Logs de OpenAI guardados en: {log_file}")
        return log_file
    return None

def clear_openai_logs():
    """Limpia los logs de OpenAI"""
    global _openai_logs
    _openai_logs = []

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
