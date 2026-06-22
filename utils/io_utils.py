"""Utilities for filesystem operations and OpenAI call logging."""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.constants import CACHE_DIR

# Global variable to store OpenAI logs
_openai_logs: List[Dict[str, Any]] = []


def setup_directories(run_path: str) -> None:
    """Create run and cache base directories if they do not exist."""
    os.makedirs(run_path, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)


def log_openai_call(
    prompt: Any,
    response: Any,
    model: str = "gpt-5",
    call_type: str = "text",
) -> None:
    """
    Log an OpenAI call for later inspection.

    Args:
        prompt: The prompt sent to OpenAI
        response: The response received from OpenAI
        model: The model used (default 'gpt-5')
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
    Save in-memory OpenAI logs to a JSON file.
    """
    if _openai_logs:
        log_file = os.path.join(run_path, "openai_logs.json")
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(_openai_logs, f, indent=2, ensure_ascii=False)
        print(f"OpenAI logs saved to: {log_file}")
        return log_file
    return None


def clear_openai_logs() -> None:
    """Clear in-memory OpenAI logs."""
    global _openai_logs
    _openai_logs = []
