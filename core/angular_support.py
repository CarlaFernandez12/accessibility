"""
Angular support helpers extracted from the legacy monolithic handler.

This module groups config discovery, template discovery, and runtime Axe helpers
used by the Angular accessibility flow without changing existing behaviour.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from core.analyzer import run_axe_analysis_with_driver
from core.webdriver_setup import setup_driver

ANGULAR_CONFIG_FILE = "angular.json"

INLINE_TEMPLATE_PATTERNS = (
    re.compile(r"template\s*:\s*`(?P<content>.*?)`", re.DOTALL),
    re.compile(r'template\s*:\s*"(?P<content>(?:\\.|[^"\\])*)"', re.DOTALL),
    re.compile(r"template\s*:\s*'(?P<content>(?:\\.|[^'\\])*)'", re.DOTALL),
)



def normalize_angular_html(html: str) -> str:
    """
    Normalise Angular-rendered HTML so it can be compared with templates.

    - Strip runtime-generated attributes (_ngcontent-*, _nghost-*, ng-reflect-*).
    - Collapse whitespace for more robust comparisons.
    """
    if not html:
        return ""

    text = html
    text = re.sub(
        r'\s(?:_ngcontent-[^= ]*|_nghost-[^= ]*|ng-reflect-[\w-]+)="[^"]*"',
        "",
        text,
    )
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_inline_template(ts_content: str) -> Optional[str]:
    """Extract inline Angular template content from a component TypeScript file."""
    if not ts_content or "template" not in ts_content:
        return None

    for pattern in INLINE_TEMPLATE_PATTERNS:
        match = pattern.search(ts_content)
        if match:
            return match.group("content")

    return None



def run_axe_on_angular_app(base_url: str, run_path: str, suffix: str = "") -> Dict:
    """
    Run Axe on an already-running Angular app and save the JSON report.
    """
    safe_suffix = suffix or ""
    report_path = Path(run_path) / f"angular_axe_report{safe_suffix}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    driver = None
    try:
        print(f"\n[Angular + Axe] Running accessibility analysis on {base_url} ...")
        driver = setup_driver()
        axe_results, driver = run_axe_analysis_with_driver(
            driver,
            base_url,
            enable_dynamic_interactions=True,
            custom_interactions=None,
        )

        with open(report_path, "w", encoding="utf-8") as file:
            json.dump(axe_results, file, indent=2, ensure_ascii=False)

        print(f"[Angular + Axe] Report saved at: {report_path}")
        return axe_results
    except Exception as exc:
        print(f"[Angular + Axe] Error running Axe: {exc}")
        raise
    finally:
        if driver:
            print("[Angular + Axe] Closing WebDriver.")
            driver.quit()



def load_angular_config(config_path: Path) -> Dict:
    with open(config_path, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def _select_default_project_name(config: Dict) -> Optional[str]:
    """Return the preferred Angular project name from an already loaded config."""
    projects = config.get("projects", {})
    if len(projects) <= 1:
        return None

    default_project = config.get("defaultProject")
    if default_project and default_project in projects:
        return default_project

    for name, project_config in projects.items():
        architect = project_config.get("architect", {})
        if "build" in architect:
            return name

    return next(iter(projects), None)


def get_default_project_name(project_root: Path) -> Optional[str]:
    """Return the default Angular project name for multi-project workspaces."""
    angular_config = project_root / ANGULAR_CONFIG_FILE
    if not angular_config.exists():
        return None

    try:
        config = load_angular_config(angular_config)
        return _select_default_project_name(config)
    except Exception:
        return None



def resolve_source_roots(project_root: Path, config: Dict) -> List[Path]:
    projects = config.get("projects", {})
    if not projects:
        return []

    source_roots: List[Path] = []

    default_project = config.get("defaultProject")
    project_names = [default_project] if default_project else []
    project_names.extend([name for name in projects.keys() if name not in project_names])

    for project_name in project_names:
        project_config = projects.get(project_name, {})
        source_root = project_config.get("sourceRoot") or project_config.get("root")
        if not source_root:
            continue
        source_path = project_root / source_root
        if source_path.exists():
            source_roots.append(source_path)

    fallback_src = project_root / "src"
    if not source_roots and fallback_src.exists():
        source_roots.append(fallback_src)

    return source_roots



def discover_component_templates(source_roots: List[Path]) -> List[Path]:
    templates: List[Path] = []
    for root in source_roots:
        templates.extend(root.glob("**/*.component.html"))
        for component_ts in root.glob("**/*.component.ts"):
            try:
                ts_content = component_ts.read_text(encoding="utf-8")
            except Exception:
                continue

            if "templateUrl" in ts_content:
                continue

            if extract_inline_template(ts_content):
                templates.append(component_ts)
    return sorted(templates)
