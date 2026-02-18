"""
Infrastructure helpers for HTTP port and dev server detection.

These utilities are used to discover active development servers (for example,
React dev servers) without changing any higher‑level flow logic.
"""

import json
import socket
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen


def _test_port(port: int) -> bool:
    """
    Check whether a local TCP port is open and responds with HTTP.

    The heuristic is:
        1. Try to open a TCP connection to localhost:port.
        2. If it succeeds, perform a simple HTTP GET and check for either
           a 2xx status code or a HTML content type.
    """
    # Check TCP connectivity
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        if sock.connect_ex(("localhost", port)) != 0:
            sock.close()
            return False
        sock.close()
    except Exception:
        return False

    # Check HTTP response
    try:
        req = Request(f"http://localhost:{port}/")
        req.add_header("User-Agent", "Mozilla/5.0")
        response = urlopen(req, timeout=3)
        content_type = response.headers.get("Content-Type", "")
        return 200 <= response.status < 300 or "text/html" in content_type.lower()
    except URLError:
        return False
    except Exception:
        return False


def detect_react_dev_server_port(project_path: str) -> Optional[int]:
    """
    Attempt to detect the development server port for a React project.

    Strategy:
        1. If a package.json exists, optionally this could inspect scripts,
           but at the moment we simply probe a list of common ports.
        2. Probe a curated list of well‑known dev ports (3000, 5173, 8080, ...).

    Returns:
        The first port that looks like an active HTTP dev server, or None.
    """
    project_root = Path(project_path)
    package_json = project_root / "package.json"

    print("[React + Axe] Detecting development server port...")

    if package_json.exists():
        # package.json is present; in the future this function can be extended
        # to inspect scripts for an explicit port. For now we still rely on
        # probing common ports.
        try:
            with package_json.open("r", encoding="utf-8") as f:
                json.load(f)
        except Exception:
            # If package.json is unreadable we still fall back to probing ports.
            pass

    common_ports = [3000, 5173, 8080, 3001, 5174, 8081, 5000, 4000, 4200, 3002]

    for port in common_ports:
        if _test_port(port):
            print(f"  ✓ Active dev server detected on port {port}")
            return port

    print("  ⚠️ No active development server detected on common ports.")
    return None

