"""
React support helpers extracted from the legacy monolithic handler.

This module groups project detection, component discovery, and runtime helpers
used by the React accessibility flow without changing existing behaviour.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.analyzer import run_axe_analysis_with_driver
from core.screenshot_handler import take_screenshots
from core.webdriver_setup import setup_driver


def _load_package_json_data(package_json: Path) -> Optional[Dict]:
    """Load package.json data when the file exists and contains valid JSON."""
    if not package_json.exists():
        return None

    try:
        with open(package_json, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return None


def normalize_react_html(html: str) -> str:
    """
    Normalise React-generated HTML so it can be compared with JSX components.

    - Removes runtime-specific attributes (data-react-*, etc.).
    - Collapses whitespace for more robust comparisons.
    """
    if not html:
        return ""

    text = html
    text = re.sub(r'\sdata-react[^= ]*="[^"]*"', "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()



def jsx_contains_html_elements(jsx_content: str, html_snippet: str) -> bool:
    """Return True when the JSX contains the main tags from the HTML snippet."""
    if not jsx_content or not html_snippet:
        return False

    normalized_jsx = normalize_react_html(jsx_content)
    normalized_html = normalize_react_html(html_snippet)

    tags = re.findall(r'<(\w+)', normalized_html)
    if not tags:
        return False

    for tag in tags:
        if f'<{tag}' not in normalized_jsx:
            return False

    return True



def detect_react_project(project_path: str) -> bool:
    """Detect whether a local project looks like a React project."""
    try:
        project_root = Path(project_path)
        package_json = project_root / "package.json"

        data = _load_package_json_data(package_json)
        if not data:
            return False

        try:
            dependencies = data.get("dependencies", {})
            dev_dependencies = data.get("devDependencies", {})
            all_dependencies = {**dependencies, **dev_dependencies}

            has_react = any(
                dependency.lower().startswith("react")
                for dependency in all_dependencies.keys()
            )
            has_jsx = any(project_root.glob("**/*.jsx")) or any(project_root.glob("**/*.tsx"))

            return has_react or has_jsx
        except (json.JSONDecodeError, KeyError):
            return False
    except Exception:
        return False

def _looks_like_react_component_source(file_path: Path) -> bool:
    """Return True when a .js/.ts source file appears to define a React component."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        print(f"[React + Axe]   ⚠️ Error leyendo {file_path}: {exc}")
        return False

    if len(content) < 30:
        return False

    has_react_import = bool(
        re.search(r'import\s+.*from\s+["\']react["\']', content, re.IGNORECASE)
    )
    has_jsx = bool(re.search(r'<[a-zA-Z]', content))
    has_return_jsx = bool(
        re.search(r'return\s+<', content) or re.search(r'return\s+\(', content)
    )
    has_export = bool(
        re.search(r'export\s+(default\s+)?(function|const|class)', content)
    )

    return (has_react_import and (has_jsx or has_return_jsx)) or (has_export and has_jsx)


def _discover_script_components(files: List[Path], skip_patterns: List[str]) -> List[Path]:
    """Filter .js/.ts files down to those that likely contain React components."""
    components: List[Path] = []
    for file_path in files:
        if any(skip in str(file_path) for skip in skip_patterns):
            continue
        if _looks_like_react_component_source(file_path):
            components.append(file_path)
    return components



def discover_react_components(source_roots: List[Path]) -> List[Path]:
    """
    Discover React component files in the project.

    Includes explicit .jsx/.tsx files and .js/.ts files that likely contain JSX.
    """
    components: List[Path] = []

    for root in source_roots:
        if not root.exists():
            print(f"[React + Axe] Warning: directory does not exist: {root}")
            continue
        if "node_modules" in str(root):
            continue

        jsx_files = [path for path in root.glob("**/*.jsx") if "node_modules" not in str(path)]
        tsx_files = [path for path in root.glob("**/*.tsx") if "node_modules" not in str(path)]
        components.extend(jsx_files)
        components.extend(tsx_files)
        print(f"[React + Axe] Found {len(jsx_files)} .jsx file(s) and {len(tsx_files)} .tsx file(s).")

        js_files = [path for path in root.glob("**/*.js") if "node_modules" not in str(path)]
        ts_files = [path for path in root.glob("**/*.ts") if "node_modules" not in str(path)]
        print(f"[React + Axe] Found {len(js_files)} .js file(s) and {len(ts_files)} .ts file(s); filtering component candidates.")

        skip_patterns = [
            "/config/",
            "/setup",
            "setupTests",
            "setupTests.js",
            "setupTests.ts",
            "reportWebVitals",
            "serviceWorker",
            "registerServiceWorker",
            "/__tests__/",
            "/test/",
            "/tests/",
            ".test.js",
            ".test.ts",
            ".spec.js",
            ".spec.ts",
        ]

        js_components = _discover_script_components(js_files, skip_patterns)
        components.extend(js_components)
        js_components_found = len(js_components)

        print(f"[React + Axe] Identified {js_components_found} .js file(s) as components.")

        ts_components = _discover_script_components(ts_files, skip_patterns)
        components.extend(ts_components)
        ts_components_found = len(ts_components)

        print(f"[React + Axe] Identified {ts_components_found} .ts file(s) as components.")

    return components



def run_axe_on_react_app(
    base_url: str,
    run_path: str,
    suffix: str = "",
    take_screenshots_flag: bool = False,
) -> Tuple[Dict, List[str]]:
    """Run Axe on an already-running React app and optionally capture screenshots."""
    driver = None
    screenshot_paths: List[str] = []

    try:
        driver = setup_driver()
        driver.get(base_url)

        if take_screenshots_flag:
            run_path_obj = Path(run_path) if isinstance(run_path, str) else run_path
            screenshot_paths = take_screenshots(
                driver,
                base_url,
                run_path_obj,
                prefix=f"screenshot{suffix}" if suffix else "screenshot",
            )

        axe_results, driver = run_axe_analysis_with_driver(driver, base_url)
        return axe_results, screenshot_paths
    finally:
        if driver:
            driver.quit()


def _npm_available() -> bool:
    """Return True when npm is available in the current environment."""
    try:
        subprocess.run(
            ["npm", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _react_port_responds(port: int) -> bool:
    """Check whether a local dev server responds with an HTML-like response on a port."""
    from urllib.request import Request, urlopen

    try:
        request = Request(f"http://localhost:{port}/")
        request.add_header("User-Agent", "Mozilla/5.0")
        response = urlopen(request, timeout=3)
        content_type = response.headers.get("Content-Type", "")
        return 200 <= response.status < 300 or "text/html" in content_type.lower()
    except Exception:
        return False


def _detect_active_react_port(common_ports: List[int]) -> Optional[int]:
    """Return the first responding local dev-server port from the candidate list."""
    for port in common_ports:
        if _react_port_responds(port):
            return port
    return None


def _choose_react_start_script(package_json: Path) -> Optional[str]:
    """Choose the preferred npm script for starting a React dev server."""
    data = _load_package_json_data(package_json)
    if not data:
        return None

    try:
        scripts = data.get("scripts", {})
        for candidate in ("start", "dev"):
            if candidate in scripts:
                return candidate
    except Exception:
        pass
    return None



def start_react_dev_server(project_root: Path) -> Optional["subprocess.Popen[str]"]:
    """
    Start the React development server in the background and wait until a port responds.
    """
    import time

    common_ports = [3000, 5173, 8080, 3001, 5174, 8081, 5000, 4000, 3002]
    max_wait_seconds = 120
    poll_interval = 3

    if not _npm_available():
        print("[React + serve-app] ⚠️ npm not found. Cannot start dev server automatically.")
        return None

    package_json = project_root / "package.json"
    if not package_json.exists():
        print("[React + serve-app] ⚠️ package.json not found. Cannot start dev server.")
        return None

    already_running = _detect_active_react_port(common_ports)
    if already_running:
        print(f"[React + serve-app] → Dev server already running on port {already_running}. Skipping start.")
        return None

    script = _choose_react_start_script(package_json)
    if not script:
        print("[React + serve-app] ⚠️ No 'start' or 'dev' script found in package.json.")
        return None

    print(f"[React + serve-app] → Starting dev server with 'npm run {script}'...")
    try:
        process = subprocess.Popen(
            ["npm", "run", script],
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception as exc:
        print(f"[React + serve-app] ⚠️ Failed to start process: {exc}")
        return None

    print(f"[React + serve-app] → Waiting for dev server (max {max_wait_seconds}s)...")
    waited = 0
    while waited < max_wait_seconds:
        time.sleep(poll_interval)
        waited += poll_interval
        active_port = _detect_active_react_port(common_ports)
        if active_port:
            print(f"[React + serve-app] ✓ Dev server ready on port {active_port} ({waited}s elapsed)")
            return process
        print(f"[React + serve-app]   Waiting... ({waited}s)")

    print(f"[React + serve-app] ⚠️ Dev server did not respond after {max_wait_seconds}s.")
    process.terminate()
    return None
