"""
Utilities for I/O and logging.

This module provides functions for directories, cache,
image-to-base64 conversion and OpenAI call logging.
"""

import base64
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.constants import CACHE_DIR, CACHE_FILE

# Global variable to store OpenAI logs
_openai_logs: List[Dict[str, Any]] = []


def setup_directories(run_path: str) -> None:
    """Create run and cache base directories if they do not exist."""
    os.makedirs(run_path, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)


def log_openai_call(
    prompt: Any,
    response: Any,
    model: str = "gpt-4o",
    call_type: str = "text",
) -> None:
    """
    Log an OpenAI call for later inspection.

    Args:
        prompt: The prompt sent to OpenAI
        response: The response received from OpenAI
        model: The model used (default 'gpt-4o')
        call_type: Call type ('text', 'vision', etc.)
    """
    log_entry: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "type": call_type,
        "model": model,
        "prompt": prompt,
        "response": response
    }
    _openai_logs.append(log_entry)


def save_openai_logs(run_path: str) -> Optional[str]:
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


def clear_openai_logs() -> None:
    """Limpia los logs de OpenAI en memoria."""
    global _openai_logs
    _openai_logs = []


def load_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_cache(cache_data: Dict[str, Any]) -> None:
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, indent=4)


def get_image_as_base64(image_path: str) -> Optional[str]:
    try:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except IOError as e:
        print(f"Error leyendo la imagen {image_path}: {e}")
        return None
