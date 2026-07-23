"""
Angular support helpers extracted from the legacy monolithic handler.

This module groups config discovery, template discovery, and runtime Axe helpers
used by the Angular accessibility flow without changing existing behaviour.
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.analyzer import run_axe_analysis_with_driver
from core.webdriver_setup import setup_driver

ANGULAR_CONFIG_FILE = "angular.json"

INLINE_TEMPLATE_PATTERNS = (
    re.compile(r"template\s*:\s*`(?P<content>.*?)`", re.DOTALL),
    re.compile(r'template\s*:\s*"(?P<content>(?:\\.|[^"\\])*)"', re.DOTALL),
    re.compile(r"template\s*:\s*'(?P<content>(?:\\.|[^'\\])*)'", re.DOTALL),
)

ANGULAR_CORE_IMPORT_REGEX = re.compile(r"from\s*['\"]@angular/core['\"]")
COMPONENT_IMPORT_REGEX = re.compile(
    r"import\s*{(?P<names>[^}]*)}\s*from\s*['\"]@angular/core['\"]",
    re.DOTALL,
)
NAMESPACE_IMPORT_REGEX = re.compile(
    r"import\s*\*\s*as\s*(?P<alias>[A-Za-z_$][\w$]*)\s*from\s*['\"]@angular/core['\"]",
    re.DOTALL,
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


def _decorator_names_for_component(ts_content: str) -> List[str]:
    """Return possible decorator identifiers that map to Angular Component."""
    decorator_names: List[str] = []

    for import_match in COMPONENT_IMPORT_REGEX.finditer(ts_content):
        names_chunk = import_match.group("names") or ""
        for part in names_chunk.split(","):
            normalized = part.strip()
            if not normalized:
                continue

            # Supports both `Component` and `Component as Alias` forms.
            alias_match = re.match(
                r"^(?P<base>Component)(?:\s+as\s+(?P<alias>[A-Za-z_$][\w$]*))?$",
                normalized,
            )
            if alias_match:
                alias = alias_match.group("alias")
                decorator_names.append(alias or "Component")

    for namespace_match in NAMESPACE_IMPORT_REGEX.finditer(ts_content):
        decorator_names.append(f"{namespace_match.group('alias')}.Component")

    return sorted(set(decorator_names))


def _extract_component_decorator_blocks(ts_content: str, decorator_names: List[str]) -> List[str]:
    """Extract object-literal blocks from @Component(...) decorators."""
    blocks: List[str] = []
    if not decorator_names:
        return blocks

    pattern = re.compile(
        r"@(" + "|".join(re.escape(name) for name in decorator_names) + r")\s*\(",
        re.DOTALL,
    )

    for match in pattern.finditer(ts_content):
        open_paren = ts_content.find("(", match.start())
        if open_paren == -1:
            continue

        object_start = ts_content.find("{", open_paren)
        if object_start == -1:
            continue

        depth = 0
        object_end = -1
        for index in range(object_start, len(ts_content)):
            char = ts_content[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    object_end = index
                    break

        if object_end == -1:
            continue

        blocks.append(ts_content[object_start : object_end + 1])

    return blocks


def _looks_like_angular_component_source(ts_content: str) -> bool:
    """Return True when a TypeScript file appears to declare an Angular component."""
    if not ts_content:
        return False
    if not ANGULAR_CORE_IMPORT_REGEX.search(ts_content):
        return False

    decorator_names = _decorator_names_for_component(ts_content)
    if not decorator_names:
        return False

    return bool(_extract_component_decorator_blocks(ts_content, decorator_names))


def _extract_component_template_reference(ts_content: str) -> Tuple[Optional[str], Optional[str]]:
    """Return an external template reference or inline template content when present."""
    decorator_names = _decorator_names_for_component(ts_content)
    for block in _extract_component_decorator_blocks(ts_content, decorator_names):
        template_url_match = re.search(r'templateUrl\s*:\s*["\']([^"\']+)["\']', block)
        if template_url_match:
            return template_url_match.group(1), None

        inline_template = extract_inline_template(block)
        if inline_template:
            return None, inline_template

    return None, None


def _resolve_source_root_path(project_root: Path, source_root: str) -> Optional[Path]:
    """Resolve sourceRoot values for classic Angular and nested Nx projects."""
    if not source_root:
        return None

    candidate = Path(source_root)
    if candidate.is_absolute() and candidate.exists():
        return candidate

    direct = (project_root / source_root).resolve()
    if direct.exists():
        return direct

    for ancestor in [project_root, *project_root.parents]:
        ancestor_candidate = (ancestor / source_root).resolve()
        if ancestor_candidate.exists():
            return ancestor_candidate

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
        source_path = _resolve_source_root_path(project_root, source_root)
        if source_path and source_path.exists():
            source_roots.append(source_path)

    fallback_src = project_root / "src"
    if not source_roots and fallback_src.exists():
        source_roots.append(fallback_src)

    return source_roots



def discover_component_templates(source_roots: List[Path]) -> List[Path]:
    templates: List[Path] = []
    for root in source_roots:
        templates.extend(root.glob("**/*.component.html"))
        for component_ts in root.glob("**/*.ts"):
            try:
                ts_content = component_ts.read_text(encoding="utf-8")
            except Exception:
                continue

            if not _looks_like_angular_component_source(ts_content):
                continue

            template_url, inline_template = _extract_component_template_reference(ts_content)
            if template_url:
                template_path = (component_ts.parent / template_url).resolve()
                if template_path.exists():
                    templates.append(template_path)
                continue

            if inline_template:
                templates.append(component_ts)

    return sorted({path.resolve() for path in templates if path.exists()})
