"""
Angular accessibility workflows and Axe-driven corrections.

This module keeps the Angular orchestration layer thin by delegating
runtime/build/accessibility details to the extracted support modules.
"""

import re
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import Request, urlopen

from core.angular_build import (
    _apply_compilation_fixes,
    _compile_and_get_errors,
    _fix_compilation_errors,
    _start_angular_dev_server,
)
from core.angular_component_processor import (
    apply_changes_map,
    process_single_component_sandbox,
)
from core.contrast_engine import (
    apply_source_catalog_contrast_repair,
    build_node_contrast_issue,
    load_color_catalog,
)
from core.angular_support import (
    ANGULAR_CONFIG_FILE,
    discover_component_templates,
    extract_inline_template,
    load_angular_config,
    normalize_angular_html,
    resolve_source_roots,
    run_axe_on_angular_app,
)
from core.screenshot_handler import take_screenshots
from core.webdriver_setup import setup_driver


DEFAULT_ANGULAR_URL = "http://localhost:4200/"


def _repair_contrast_issues_at_source(
    axe_results: Dict,
    project_root: Path,
    analysis_results_path: Optional[str],
) -> Dict:
    if not analysis_results_path:
        return axe_results

    catalog_path = str(Path(analysis_results_path).with_name("color_catalog.json"))
    color_catalog = load_color_catalog(catalog_path)
    if not color_catalog:
        return axe_results

    repaired_count = 0
    modified_files = set()
    remaining_violations: List[Dict] = []

    for violation in axe_results.get("violations", []) or []:
        if violation.get("id") != "color-contrast":
            remaining_violations.append(violation)
            continue

        remaining_nodes = []
        for node in violation.get("nodes", []) or []:
            issue = build_node_contrast_issue(violation, node)
            repair = apply_source_catalog_contrast_repair(issue, color_catalog, str(project_root))
            if repair:
                repaired_count += 1
                modified_files.add(repair.get("sourceFile"))
            else:
                remaining_nodes.append(node)

        if remaining_nodes:
            remaining_violation = dict(violation)
            remaining_violation["nodes"] = remaining_nodes
            remaining_violations.append(remaining_violation)

    if repaired_count:
        print(
            f"[Angular + Contrast] Repaired {repaired_count} contrast issue(s) at source "
            f"across {len(modified_files)} file(s) using the color catalog"
        )

    updated_results = dict(axe_results)
    updated_results["violations"] = remaining_violations
    return updated_results


def fix_angular_project_from_axe_results(
    project_path: str,
    axe_results: dict,
    client,
    run_path: str,
    analysis_results_path: Optional[str] = None,
) -> None:
    """Apply Angular source fixes from a previously captured Axe report."""
    print("[Angular] Fixing source files from saved analysis results...")

    project_root = Path(project_path)
    axe_results = _repair_contrast_issues_at_source(axe_results, project_root, analysis_results_path)
    issues_by_template = map_axe_violations_to_templates(axe_results, project_root)
    if not issues_by_template:
        print("[Angular] No violations were mapped to templates.")
        return

    fixes = fix_templates_with_axe_violations(issues_by_template, project_root, client)
    print(f"[Angular] Fix completed. Templates updated: {len(fixes)}")


def _discover_project_templates(project_root: Path) -> List[Path]:
    angular_config = project_root / ANGULAR_CONFIG_FILE
    source_roots: List[Path] = []

    if angular_config.exists():
        try:
            config_data = load_angular_config(angular_config)
            source_roots = resolve_source_roots(project_root, config_data)
        except Exception as exc:
            print(f"[Angular] Warning: failed to load angular.json: {exc}")

    templates = discover_component_templates(source_roots)
    if templates:
        return templates

    fallback_templates = sorted(project_root.glob("**/*.component.html"))
    return [path for path in fallback_templates if "node_modules" not in str(path)]


def map_axe_violations_to_templates(
    axe_results: Dict,
    project_root: Path,
    source_roots: Optional[List[Path]] = None,
) -> Dict[str, List[Dict]]:
    """Map rendered Axe violations back to component templates by HTML snippet matching."""
    if not axe_results:
        return {}

    violations = axe_results.get("violations", []) or []
    if not violations:
        return {}

    if source_roots is None:
        templates = _discover_project_templates(project_root)
    else:
        templates = discover_component_templates(source_roots)

    template_cache: Dict[str, str] = {}
    for template_path in templates:
        try:
            rel_path = str(template_path.relative_to(project_root))
            raw_content = template_path.read_text(encoding="utf-8")
            if template_path.suffix == ".ts":
                raw_content = extract_inline_template(raw_content) or ""
            template_cache[rel_path] = normalize_angular_html(raw_content)
        except Exception:
            continue

    issues_by_template: Dict[str, List[Dict]] = {}

    for violation in violations:
        violation_id = violation.get("id", "unknown")
        for node in violation.get("nodes", []) or []:
            html_snippet = (node.get("html") or "").strip()
            target_selectors = node.get("target", []) or []
            if violation_id == "html-has-lang":
                index_path = project_root / "src/index.html"
                if index_path.exists():
                    rel_path = str(index_path.relative_to(project_root))
                    issues_by_template.setdefault(rel_path, []).append(
                        {
                            "violation": violation,
                            "violation_id": violation_id,
                            "node": node,
                        }
                    )
                continue

            if not html_snippet:
                continue

            normalized_snippet = normalize_angular_html(html_snippet)
            if not normalized_snippet:
                continue

            matched_rel_path: Optional[str] = None

            for rel_path, normalized_template in template_cache.items():
                if normalized_snippet in normalized_template:
                    matched_rel_path = rel_path
                    break

            if matched_rel_path is None and target_selectors:
                for selector in target_selectors:
                    selector = (selector or "").strip()
                    if not selector.startswith("."):
                        selector_lower = selector.lower()
                        if "> img" in selector_lower:
                            for rel_path, normalized_template in template_cache.items():
                                if (
                                    "<img" in normalized_template
                                    and ("<a" in normalized_template or "routerlink" in normalized_template)
                                    and (
                                        "article.author" in normalized_template
                                        or "article-meta" in normalized_template
                                    )
                                ):
                                    matched_rel_path = rel_path
                                    break
                            if matched_rel_path is not None:
                                break
                        continue

                    class_name = re.split(r"[\s>:\[]", selector[1:], 1)[0]
                    if not class_name:
                        continue

                    class_pattern = f'class="{class_name}"'
                    class_attr_pattern = f' {class_name} '
                    for rel_path, normalized_template in template_cache.items():
                        if class_pattern in normalized_template or class_attr_pattern in f" {normalized_template} ":
                            matched_rel_path = rel_path
                            break
                    if matched_rel_path is not None:
                        break

            if matched_rel_path is None:
                visible_text = re.sub(r"<[^>]+>", " ", html_snippet)
                visible_text = re.sub(r"\s+", " ", visible_text).strip()
                if visible_text and len(visible_text) >= 3:
                    for rel_path, normalized_template in template_cache.items():
                        if visible_text in normalized_template:
                            matched_rel_path = rel_path
                            break

            if matched_rel_path is not None:
                issues_by_template.setdefault(matched_rel_path, []).append(
                    {
                        "violation": violation,
                        "violation_id": violation_id,
                        "node": node,
                    }
                )

    return issues_by_template


def _build_axe_based_prompt_for_template(
    template_path: str,
    template_content: str,
    issues: List[Dict],
) -> str:
    """Build a compact prompt summary for an Angular template and its Axe issues."""
    violation_lines: List[str] = []

    for issue in issues:
        violation = issue.get("violation", {}) or {}
        node = issue.get("node", {}) or {}

        violation_id = violation.get("id", "unknown")
        impact = violation.get("impact", "moderate")
        description = violation.get("description", "")
        html_snippet = (node.get("html") or "").strip()

        tag = "element"
        tag_match = re.search(r"<(\w+)", html_snippet)
        if tag_match:
            tag = tag_match.group(1)

        line = f"- {violation_id} ({impact}) en <{tag}>"
        if description:
            line += f": {description}"
        violation_lines.append(line)

        if html_snippet:
            violation_lines.append(f"  HTML: {html_snippet.splitlines()[0].strip()[:200]}...")

    return (
        f"Fix ALL {len(issues)} WCAG A/AA violations in this Angular template.\n\n"
        f"TEMPLATE: {template_path}\n\n"
        f"VIOLATIONS:\n{'\n'.join(violation_lines)}\n\n"
        "INSTRUCTIONS:\n"
        "- Fix only the listed elements.\n"
        "- Keep Angular bindings intact.\n"
        "- Do not change layout or responsive classes.\n\n"
        "FULL CURRENT TEMPLATE:\n"
        f"```html\n{template_content}\n```"
    )


def fix_templates_with_axe_violations(
    issues_by_template: Dict[str, List[Dict]],
    project_root: Path,
    client,
    screenshot_paths: Optional[List[str]] = None,
) -> Dict[str, Dict[str, str]]:
    """Apply Axe-guided fixes to mapped Angular templates through the component processor."""
    fixes: Dict[str, Dict[str, str]] = {}
    changes_map: List[Dict] = []
    failures: List[str] = []

    if not issues_by_template:
        print("[Angular + Axe] No violations mapped to templates.")
        return fixes

    for rel_path, issues in issues_by_template.items():
        template_path = project_root / rel_path
        if not template_path.exists():
            print(f"[Angular + Axe] ⚠️ Template not found: {rel_path}")
            failures.append(f"Template not found: {rel_path}")
            continue

        try:
            original_content = template_path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[Angular + Axe] Warning: failed to read {rel_path}: {exc}")
            failures.append(f"Failed to read {rel_path}: {exc}")
            continue

        result, changes = process_single_component_sandbox(
            template_path,
            client,
            project_root,
            axe_errors=issues,
            screenshot_paths=screenshot_paths,
        )
        if not changes:
            continue

        changes_map.append(
            {
                "component_name": result.get("component_name"),
                "template_path": result.get("template_path"),
                "changes": changes,
            }
        )
        fixes[rel_path] = {
            "original": original_content,
            "corrected": changes.get("template", original_content),
        }

    if changes_map:
        applied_count = apply_changes_map(changes_map)
        print(f"[Angular + Axe] Applied changes to {applied_count} file(s).")

    if failures:
        raise RuntimeError(
            "Angular fix flow could not complete successfully for all templates: "
            + "; ".join(failures)
        )

    return fixes


def _process_templates_without_axe(
    templates: List[Path],
    project_root: Path,
    client,
    screenshot_paths: Optional[List[str]] = None,
) -> List[Dict]:
    changes_map: List[Dict] = []

    for template_path in templates:
        result, changes = process_single_component_sandbox(
            template_path,
            client,
            project_root,
            axe_errors=None,
            screenshot_paths=screenshot_paths,
        )
        if changes:
            changes_map.append(
                {
                    "component_name": result.get("component_name"),
                    "template_path": result.get("template_path"),
                    "changes": changes,
                }
            )

    if changes_map:
        applied_count = apply_changes_map(changes_map)
        print(f"[Angular] Applied changes to {applied_count} file(s).")

    return changes_map


def _wait_for_url(url: str, timeout_seconds: int = 60) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            request = Request(url)
            request.add_header("User-Agent", "Mozilla/5.0")
            response = urlopen(request, timeout=3)
            if 200 <= response.status < 500:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _capture_angular_screenshots(base_url: str, run_path: str) -> List[str]:
    driver = None
    try:
        driver = setup_driver()
        return take_screenshots(driver, base_url, Path(run_path), prefix="angular_before")
    except Exception as exc:
        print(f"[Angular + Axe] Warning: screenshot capture failed: {exc}")
        return []
    finally:
        if driver:
            driver.quit()


def _run_live_angular_axe_flow(
    project_root: Path,
    client,
    run_path: str,
) -> Tuple[List[str], int, str]:
    """Run the live Angular Axe flow against the local dev server."""
    if not _wait_for_url(DEFAULT_ANGULAR_URL):
        return [], 0, "Angular app did not respond in time; skipping the live Axe flow."

    screenshot_paths = _capture_angular_screenshots(DEFAULT_ANGULAR_URL, run_path)
    axe_results = run_axe_on_angular_app(DEFAULT_ANGULAR_URL, run_path, suffix="_before")
    issues_by_template = map_axe_violations_to_templates(axe_results, project_root)
    live_fixes = fix_templates_with_axe_violations(
        issues_by_template,
        project_root,
        client,
        screenshot_paths=screenshot_paths,
    )
    live_fix_count = len(live_fixes)
    return screenshot_paths, live_fix_count, f"Templates fixed with Axe: {live_fix_count}"


def _run_angular_build_stage(
    project_root: Path,
    client,
    stage_name: str,
    fixes_summary_label: str,
) -> List[str]:
    """Run one Angular build verification stage and apply compilation fixes when needed."""
    summary: List[str] = []
    build_result = _compile_and_get_errors(project_root)

    if build_result["verification_available"]:
        summary.append(
            f"Build {stage_name}: OK"
            if build_result["success"]
            else f"Build {stage_name}: {len(build_result['errors'])} error(s)"
        )
    else:
        summary.append(f"Build {stage_name}: verification unavailable")

    if build_result["errors"]:
        compilation_fixes = _fix_compilation_errors(build_result["errors"], project_root, client)
        if compilation_fixes:
            _apply_compilation_fixes(compilation_fixes, project_root)
            summary.append(f"{fixes_summary_label}: {len(compilation_fixes)}")

    return summary


def process_angular_project(project_path: str, client, run_path: str, serve_app: bool = False) -> List[str]:
    """Run the Angular remediation flow and return a short execution summary."""
    project_root = Path(project_path)
    summary: List[str] = []

    templates = _discover_project_templates(project_root)
    summary.append(f"Templates found: {len(templates)}")
    summary.extend(
        _run_angular_build_stage(
            project_root,
            client,
            "inicial",
            "Fixes de compilación aplicados",
        )
    )

    screenshot_paths: List[str] = []
    live_fix_count = 0
    static_fix_count = 0
    server_process = None

    try:
        if serve_app:
            print("[Angular + serve-app] Starting Angular dev server...")
            server_process = _start_angular_dev_server(project_root, wait_for_ready=True)
            screenshot_paths, live_fix_count, live_summary = _run_live_angular_axe_flow(
                project_root,
                client,
                run_path,
            )
            summary.append(live_summary)

        if not serve_app or live_fix_count == 0:
            static_fixes = _process_templates_without_axe(
                templates,
                project_root,
                client,
                screenshot_paths=screenshot_paths or None,
            )
            static_fix_count = len(static_fixes)
            summary.append(f"Templates fixed by static analysis: {static_fix_count}")

        summary.extend(
            _run_angular_build_stage(
                project_root,
                client,
                "final",
                "Fixes finales de compilación aplicados",
            )
        )
    finally:
        if server_process is not None:
            try:
                server_process.terminate()
            except Exception:
                pass

    return summary


