"""
Angular accessibility workflows and Axe‑driven corrections.

This module contains all logic specific to analysing and fixing Angular
applications, including:
    - Discovering templates and mapping Axe violations to them.
    - Running Axe against a running Angular dev server.
    - Guiding LLM‑driven corrections and optional automatic contrast fixes.

Business behaviour must remain stable; refactors focus on structure,
type hints and documentation only.
"""

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.io_utils import log_openai_call
from core.webdriver_setup import setup_driver
from core.analyzer import run_axe_analysis
from core.screenshot_handler import take_screenshots, create_screenshot_summary

ANGULAR_CONFIG_FILE = "angular.json"

# Feature flag for automatic contrast corrections in Angular.
# Before introducing these automatic fixes, the Angular flow relied almost
# entirely on the LLM and behaved more predictably. To avoid regressions
# (for example, always adding `color: #000000` over dark backgrounds),
# this remains disabled by default.
ENABLE_AUTOMATIC_CONTRAST_FIXES = False


def _normalize_angular_html(html: str) -> str:
    """
    Normalise Angular-rendered HTML so it can be compared with templates.

    - Strip runtime-generated attributes (_ngcontent-*, _nghost-*, ng-reflect-*, etc.)
    - Collapse whitespace for more robust comparisons.
    """
    if not html:
        return ""

    import re

    text = html
    # Strip Angular runtime "noise" attributes from rendered DOM
    text = re.sub(r'\s(?:_ngcontent-[^= ]*|_nghost-[^= ]*|ng-reflect-[\w-]+)="[^"]*"', "", text)
    # Normalise whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def run_axe_on_angular_app(base_url: str, run_path: str, suffix: str = "") -> Dict:
    """
    Run Axe on an already-running Angular app (e.g. http://localhost:4200/)
    and save the report as JSON in the current run's results directory.

    Args:
        base_url: Base URL where the Angular app is served (e.g. http://localhost:4200/).
        run_path: Current run's results directory.
        suffix: Optional suffix to distinguish reports (e.g. "_before", "_after").

    NOTE: This function assumes the Angular project is already serving the app
    (e.g. via `ng serve` or `npm start`) at the given base_url. It does not
    modify any project files; it only returns and saves Axe results.
    """
    safe_suffix = suffix or ""
    report_path = Path(run_path) / f"angular_axe_report{safe_suffix}.json"

    driver = None
    try:
        print(f"\n[Angular + Axe] Running accessibility analysis on {base_url} ...")
        driver = setup_driver()
        axe_results = run_axe_analysis(
            driver,
            base_url,
            enable_dynamic_interactions=True,
            custom_interactions=None,
        )

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(axe_results, f, indent=2, ensure_ascii=False)

        print(f"[Angular + Axe] Report saved at: {report_path}")
        return axe_results
    except Exception as e:
        print(f"[Angular + Axe] Error running Axe: {e}")
        raise
    finally:
        if driver:
            print("[Angular + Axe] Closing WebDriver.")
            driver.quit()


def map_axe_violations_to_templates(
    axe_results: Dict, project_root: Path, source_roots: Optional[List[Path]] = None
) -> Dict[str, List[Dict]]:
    """
    Map Axe violations (on rendered HTML) to Angular templates (*.component.html).

    Strategy: for each violating node we use the HTML fragment (`html`) from Axe,
    normalise it and template content to strip Angular runtime attributes
    (_ngcontent-*, _nghost-*, etc.), and match by substring to associate
    violations with template files.

    Returns:
        Dict[str, List[Dict]] keyed by template path (relative to project_root),
        values are lists of violation/node info dicts.
    """
    if not axe_results:
        return {}

    violations = axe_results.get("violations", []) or []
    if not violations:
        return {}

    # Resolve source_roots if not provided
    if source_roots is None:
        angular_config = project_root / ANGULAR_CONFIG_FILE
        if angular_config.exists():
            config_data = _load_angular_config(angular_config)
            source_roots = _resolve_source_roots(project_root, config_data)
        else:
            # Fallback: look in common locations
            possible_roots = [
                project_root / "src",
                project_root / "app",
                project_root,
            ]
            source_roots = [r for r in possible_roots if r.exists()]
            if not source_roots:
                print(f"[Angular + Axe] ⚠️ angular.json and common dirs (src/, app/) not found")
                print(f"[Angular + Axe] Searching for templates across the whole project...")
                source_roots = [project_root]

    # Load all templates in memory: relative path -> {"normalized": str, "raw": str}
    templates: Dict[str, Dict[str, str]] = {}
    for root in source_roots:
        # Include component templates (*.component.html)
        for tpl_path in root.glob("**/*.component.html"):
            try:
                raw = tpl_path.read_text(encoding="utf-8")
                normalized = _normalize_angular_html(raw)
                rel = str(tpl_path.relative_to(project_root))
                templates[rel] = {"normalized": normalized, "raw": raw}
            except Exception:
                continue

        # Also include INLINE templates in TypeScript files (@Component({ template: `...` }))
        for ts_path in root.glob("**/*.component.ts"):
            try:
                ts_raw = ts_path.read_text(encoding="utf-8")
            except Exception:
                continue

            import re

            # Find template: ` ... ` inside @Component({ ... })
            # Simple but effective pattern: template: `...`
            inline_matches = re.findall(
                r"template\s*:\s*`([\s\S]*?)`",
                ts_raw,
                flags=re.MULTILINE,
            )
            if not inline_matches:
                continue

            for idx, inline_tpl in enumerate(inline_matches, start=1):
                normalized = _normalize_angular_html(inline_tpl)
                # Use a virtual name for this inline template, tied to the .ts file
                rel = str(ts_path.relative_to(project_root)) + f"::inline_template_{idx}"
                templates[rel] = {"normalized": normalized, "raw": inline_tpl}
    
    # Debug: show how many templates were found
    if not templates:
        print(f"[Angular + Axe] ⚠️ No templates (*.component.html) found in:")
        for root in source_roots:
            print(f"  - {root}")
        print(f"[Angular + Axe] Searching across the whole project...")
        # More aggressive search: scan entire project
        for tpl_path in project_root.rglob("*.component.html"):
            try:
                raw = tpl_path.read_text(encoding="utf-8")
                normalized = _normalize_angular_html(raw)
                rel = str(tpl_path.relative_to(project_root))
                templates[rel] = {"normalized": normalized, "raw": raw}
            except Exception:
                continue
    
    if templates:
        print(f"[Angular + Axe] ✓ Found {len(templates)} template(s) to map violations")
    else:
        print(f"[Angular + Axe] ⚠️ No templates found. Mapping may fail.")

    # Also include index.html and other static HTML files in src/
    src_dir = project_root / "src"
    if src_dir.exists():
        # Find index.html
        index_html = src_dir / "index.html"
        if index_html.exists():
            try:
                raw = index_html.read_text(encoding="utf-8")
                normalized = _normalize_angular_html(raw)
                rel = str(index_html.relative_to(project_root))
                templates[rel] = {"normalized": normalized, "raw": raw}
            except Exception:
                pass
        
        # Find other static HTML files (not components)
        for html_path in src_dir.rglob("*.html"):
            # Exclude components (already processed) and node_modules
            if "node_modules" in str(html_path) or html_path.name.endswith(".component.html"):
                continue
            if html_path == index_html:  # Already processed
                continue
            try:
                raw = html_path.read_text(encoding="utf-8")
                normalized = _normalize_angular_html(raw)
                rel = str(html_path.relative_to(project_root))
                templates[rel] = {"normalized": normalized, "raw": raw}
            except Exception:
                continue

    issues_by_template: Dict[str, List[Dict]] = {}

    for violation in violations:
        violation_id = violation.get("id", "")
        for node in violation.get("nodes", []):
            html_snippet = node.get("html") or ""
            if not html_snippet:
                continue

            normalized_snippet = _normalize_angular_html(html_snippet)
            if not normalized_snippet.strip():
                continue

            matched_template = None

            # 1) Search on normalised HTML
            for rel_path, tpl_data in templates.items():
                if normalized_snippet in tpl_data["normalized"]:
                    # VALIDATION: ensure the snippet's main element is actually in the template
                    snippet_tag = re.search(r'<(\w+)', html_snippet)
                    if snippet_tag:
                        tag_name = snippet_tag.group(1)
                        if f'<{tag_name}' in tpl_data["raw"] or f'<{tag_name} ' in tpl_data["raw"]:
                            matched_template = rel_path
                            break

            # 2) Fallback: try original fragment (unnormalised)
            if not matched_template:
                raw_snippet = html_snippet.strip()
                for rel_path, tpl_data in templates.items():
                    if raw_snippet and raw_snippet in tpl_data["raw"]:
                        # VALIDATION: ensure main element is in the template
                        snippet_tag = re.search(r'<(\w+)', raw_snippet)
                        if snippet_tag:
                            tag_name = snippet_tag.group(1)
                            if f'<{tag_name}' in tpl_data["raw"] or f'<{tag_name} ' in tpl_data["raw"]:
                                matched_template = rel_path
                                break

            # 3) Extra step: try Axe CSS selector (classes/ids) to locate the template
            if not matched_template:
                targets = node.get("target") or []
                selector = targets[0] if targets and isinstance(targets[0], str) else None

                if selector:
                    import re

                    # Special case: errors on root elements like <html>
                    if selector == "html" and violation_id == "html-has-lang":
                        # Look for index.html specifically
                        for rel_path in templates.keys():
                            if "index.html" in rel_path:
                                matched_template = rel_path
                                break
                        if matched_template:
                            pass

                    if not matched_template:
                        classes = re.findall(r"\.([a-zA-Z0-9_-]+)", selector)
                        ids = re.findall(r"#([a-zA-Z0-9_-]+)", selector)
                        # Also match element names (no . or #)
                        element_names = re.findall(r"^([a-zA-Z][a-zA-Z0-9-]*)(?=[\.#\s>+~:\[\]()]|$)", selector)

                        candidate_paths = []
                        for rel_path, tpl_data in templates.items():
                            raw_tpl = tpl_data["raw"]

                            # Buscar por nombres de elementos (ej: "html", "body", "nb-icon")
                            if element_names:
                                element_found = False
                                for elem_name in element_names:
                                    # Find element in template (may have attributes)
                                    if f"<{elem_name}" in raw_tpl or f"<{elem_name} " in raw_tpl or f"<{elem_name}>" in raw_tpl:
                                        element_found = True
                                        break
                                if not element_found:
                                    continue

                            # All selector classes must appear in the template
                            if classes and not all(cls in raw_tpl for cls in classes):
                                continue

                            # All selector ids must appear in the template
                            if ids:
                                has_all_ids = True
                                for id_value in ids:
                                    if (
                                        f'id="{id_value}"' not in raw_tpl
                                        and f"id='{id_value}'" not in raw_tpl
                                    ):
                                        has_all_ids = False
                                        break
                                if not has_all_ids:
                                    continue

                            if classes or ids or element_names:
                                candidate_paths.append(rel_path)

                        # If only one clear candidate, use it
                        if len(candidate_paths) == 1:
                            matched_template = candidate_paths[0]
                        # If multiple candidates and one is index.html with html-has-lang, use index.html
                        elif len(candidate_paths) > 1 and violation_id == "html-has-lang":
                            for rel_path in candidate_paths:
                                if "index.html" in rel_path:
                                    matched_template = rel_path
                                    break
                        # If multiple candidates and not special case, associate violation with ALL
                        elif len(candidate_paths) > 1:
                            for rel_path in candidate_paths:
                                entry = {
                                    "violation_id": violation_id,
                                    "violation": violation,
                                    "node": node,
                                }
                                issues_by_template.setdefault(rel_path, []).append(entry)
                            continue

            if not matched_template:
                continue

            entry = {
                "violation_id": violation_id,
                "violation": violation,
                "node": node,
            }
            issues_by_template.setdefault(matched_template, []).append(entry)

    return issues_by_template


def fix_css_with_axe(
    axe_results: Dict, project_root: Path, client
) -> Dict[str, Dict[str, str]]:
    """
    Apply Axe-based contrast fixes at global CSS level.

    Initial conservative behaviour:
    - Only handles 'color-contrast' violations.
    - Only considers simple class selectors (e.g. '.navbar-brand').
    - Only adds new CSS rules for those selectors at the end of
      'src/styles.scss' (or 'src/styles.css' if the former does not exist).
    - Does not change layout (display, flex, grid, etc.), only color / background-color
      and optionally font-weight.
    """
    fixes: Dict[str, Dict[str, str]] = {}

    if not axe_results:
        return fixes

    violations = axe_results.get("violations", []) or []
    if not violations:
        return fixes

    # Locate main global stylesheet
    styles_scss = project_root / "src" / "styles.scss"
    styles_css = project_root / "src" / "styles.css"
    if styles_scss.exists():
        styles_path = styles_scss
    elif styles_css.exists():
        styles_path = styles_css
    else:
        # No standard global styles, do nothing
        return fixes

    try:
        original_styles = styles_path.read_text(encoding="utf-8")
    except Exception:
        return fixes

    # Group contrast violations by simple (class) selector
    from collections import defaultdict
    import re

    issues_by_selector: Dict[str, List[Dict]] = defaultdict(list)
    
    # Overly generic selectors we must NOT use (would break layout)
    GENERIC_SELECTORS_BLACKLIST = {
        ".btn", ".container", ".row", ".col", ".card", ".nav", ".navbar",
        ".form", ".input", ".label", ".text", ".title", ".header", ".footer",
        ".main", ".content", ".wrapper", ".section", ".div", ".span", ".p",
        ".a", ".button", ".img", ".ul", ".li", ".table", ".tr", ".td"
    }

    for violation in violations:
        if violation.get("id") != "color-contrast":
            continue
        for node in violation.get("nodes", []):
            # Try to derive a CSS selector from the element's class
            html = node.get("html") or ""
            targets = node.get("target") or []

            selector = None

            # 1) Extract ALL classes from HTML and pick the MOST SPECIFIC (not the first)
            class_match = re.search(r'class=["\']([^"\']+)["\']', html)
            if class_match:
                classes_in_html = class_match.group(1).split()
                if classes_in_html:
                    # Prefer more specific classes (not in blacklist)
                    # e.g. "btn btn-primary" -> prefer ".btn-primary" over ".btn"
                    for cls in reversed(classes_in_html):  # Start from last (most specific)
                        candidate = f".{cls}"
                        if candidate not in GENERIC_SELECTORS_BLACKLIST:
                            selector = candidate
                            break
                    # If all in blacklist, use last anyway (better than nothing)
                    if not selector and classes_in_html:
                        selector = f".{classes_in_html[-1]}"

            # 2) If no class in HTML, use Axe target if it's a simple class
            if not selector and targets and isinstance(targets[0], str):
                raw_selector = targets[0].strip()
                # Extract only the class part of the selector (ignore attributes, pseudo-classes, etc.)
                class_parts = re.findall(r'\.([a-zA-Z0-9_-]+)', raw_selector)
                if class_parts:
                    # Use the last class found (most specific)
                    selector = f".{class_parts[-1]}"
                    if selector in GENERIC_SELECTORS_BLACKLIST:
                        # If generic, try the previous one
                        if len(class_parts) > 1:
                            selector = f".{class_parts[-2]}"
                        else:
                            selector = None  # Discard if only one generic class

            if not selector or selector in GENERIC_SELECTORS_BLACKLIST:
                continue

            # Extract contrast data from first relevant entry
            contrast_data = None
            any_checks = node.get("any", []) or []
            for check in any_checks:
                data = check.get("data")
                if isinstance(data, dict) and data.get("bgColor") and data.get("fgColor"):
                    contrast_data = data
                    break

            issues_by_selector[selector].append(
                {
                    "violation": violation,
                    "node": node,
                    "contrast": contrast_data,
                }
            )

    if not issues_by_selector:
        return fixes

    updated_css_blocks: List[str] = []

    for selector, issues in issues_by_selector.items():
        # Build problem text for the prompt
        problems_lines: List[str] = []
        for issue in issues:
            data = (issue.get("contrast") or {}) if issue.get("contrast") else {}
            bg = data.get("bgColor")
            fg = data.get("fgColor")
            ratio = data.get("contrastRatio")
            expected = data.get("expectedContrastRatio")
            problems_lines.append(
                f"- Selector: {selector} | bgColor: {bg} | fgColor: {fg} | "
                f"ratio: {ratio} | ratio requerido: {expected}"
            )

        problems_text = "\n".join(problems_lines)

        # Check if a rule for this selector already exists (avoid duplicates)
        selector_exists = re.search(rf'\.{re.escape(selector.lstrip("."))}\s*\{{', original_styles, re.IGNORECASE)
        existing_note = ""
        if selector_exists:
            existing_note = f"\n⚠️ IMPORTANTE: Ya existe una regla para {selector} en el CSS. Tu nueva regla DEBE usar !important para sobrescribirla."

        prompt = f"""
Tienes un proyecto Angular con Bootstrap. Axe ha detectado ERRORES DE CONTRASTE (regla color-contrast)
para el selector CSS {selector}.

DETALLES DE LAS VIOLACIONES (PUEDEN SER VARIAS INSTANCIAS):
{problems_text}
{existing_note}

HOJA DE ESTILOS GLOBAL ACTUAL (resumen):
```css
{original_styles[:4000]}
```

CRITICAL TASK:
- You must propose new CSS rules for the selector {selector} (and only for it) that fix
  ALL the indicated contrast errors.
- Since this project uses Bootstrap, you MUST use !important on color so that
  your rules override Bootstrap styles.
- 🚨 IMPORTANT: Do NOT use `background-color` unless absolutely necessary.
  Bootstrap already handles backgrounds correctly. Only adjust the text `color`.
- Do NOT change layout: do NOT touch display, position, flex, grid, width, height,
  margin, padding, align-items, justify-content, etc.
- YOU MAY ONLY MODIFY OR ADD:
  - color (with !important) - REQUIRED
  - font-weight (optional, only if it really helps readability)
- Choose colours that meet at least the required ratio (4.5:1 for normal text, 3:1 for large text).
- For dark backgrounds (#007bff, #17a2b8, etc.), use light text (#ffffff or similar).
- For light backgrounds, use dark text (#000000, #212121, etc.).

MANDATORY RESPONSE FORMAT:
Return EXCLUSIVELY a CSS block ready to PASTE at the end of styles.css/styles.scss,
DELIMITED by:

<<<UPDATED_CSS>>>
{selector} {{
  color: #XXXXXX !important;
}}
<<<END_UPDATED_CSS>>>

NOTE: Include only `color`, do NOT include `background-color` unless absolutely critical.

Do NOT include explanations, markdown, or ```css```, only the block between the markers.
""".strip()

        system_message = (
            "You are an accessibility (WCAG 2.2 AA) and CSS expert. "
            "Your task is to adjust text/background colours to improve contrast "
            "WITHOUT changing layout or breaking the overall design."
        )

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
            content = response.choices[0].message.content or ""
            log_openai_call(
                prompt=prompt,
                response=content,
                model="gpt-4o",
                call_type="angular_axe_css_fix",
            )

            # Extract UPDATED_CSS block
            start_marker = "<<<UPDATED_CSS>>>"
            end_marker = "<<<END_UPDATED_CSS>>>"
            start_idx = content.find(start_marker)
            end_idx = content.find(end_marker)
            if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
                continue

            updated_block = content[start_idx + len(start_marker) : end_idx].strip()
            if not updated_block:
                continue

            # Basic validation: avoid dangerous layout properties
            forbidden_props = [
                "display:",
                "position:",
                "flex:",
                "grid:",
                "width:",
                "height:",
                "margin:",
                "padding:",
                "top:",
                "left:",
                "right:",
                "bottom:",
            ]
            lower_block = updated_block.lower()
            if any(prop in lower_block for prop in forbidden_props):
                continue

            updated_css_blocks.append(
                f"/* Axe-based contrast fix para {selector} */\n{updated_block}\n"
            )

        except Exception as e:
            print(f"[Angular + Axe CSS] ⚠️ Error fixing selector {selector}: {e}")
            continue

    if not updated_css_blocks:
        return fixes

    # Remove old "Axe-based contrast fix" rules to avoid accumulation
    # Use regex to strip blocks starting with "/* Axe-based contrast fix" until next block or end
    axe_block_pattern = r'/\* Axe-based contrast fix para[^*]*\*/(?:[^*]|\*(?!/))*?}'
    cleaned_styles = re.sub(axe_block_pattern, '', original_styles, flags=re.DOTALL)
    # Collapse multiple blank lines
    cleaned_styles = re.sub(r'\n\s*\n\s*\n+', '\n\n', cleaned_styles).rstrip()

    new_styles = cleaned_styles + "\n\n" + "\n\n".join(updated_css_blocks) + "\n"
    if new_styles != original_styles:
        try:
            styles_path.write_text(new_styles, encoding="utf-8")
            fixes[str(styles_path)] = {
                "original": original_styles,
                "corrected": new_styles,
            }
            print(
                f"[Angular + Axe CSS] ✓ Added {len(updated_css_blocks)} contrast rules in {styles_path}"
            )
        except Exception as e:
            print(f"[Angular + Axe CSS] ⚠️ Could not write to {styles_path}: {e}")

    return fixes


def _build_axe_based_prompt_for_template(
    template_path: str, template_content: str, issues: List[Dict]
) -> str:
    """
    Prompt compacto para corregir accesibilidad en un template Angular
    a partir de las violaciones de Axe.
    """
    violations_lines: List[str] = []

    for issue in issues:
        violation = issue.get("violation", {}) or {}
        node = issue.get("node", {}) or {}

        v_id = violation.get("id", "unknown")
        impact = violation.get("impact", "moderate")
        desc = violation.get("description", "")
        html_snippet = (node.get("html") or "").strip()

        # Main tag of the snippet (so the model knows what to look for)
        tag = "elemento"
        m = re.search(r"<(\w+)", html_snippet)
        if m:
            tag = m.group(1)

        # Main violation line
        line = f"- {v_id} ({impact}) en <{tag}>"
        if desc:
            line += f": {desc}"
        violations_lines.append(line)

        # Add a single HTML line for reference
        if html_snippet:
            first_line = html_snippet.splitlines()[0].strip()
            violations_lines.append(f"  HTML: {first_line[:200]}...")

    violations_text = "\n".join(violations_lines)
    total = len(issues)

    prompt = f"""Fix ALL {total} WCAG A/AA violations in this Angular template.

TEMPLATE: {template_path}

VIOLATIONS:
{violations_text}

QUICK RULES:
- button-name → add visible text or aria-label="..." to <button>
- color-contrast → adjust ONLY style="color:#000000" or "#FFFFFF" according to background
- link-name → add descriptive text or aria-label="..." to <a>
- image-alt / role-img-alt → add alt="..." or aria-label="..." to the visual element
- frame-title → add title="..." to <iframe>
- aria-* → add/fix aria attributes (aria-label, aria-labelledby, etc.)

INSTRUCTIONS:
- Fix ONLY the elements listed in the violations list.
- Keep *ngIf, *ngFor, bindings and pipes intact.
- Do not change layout or responsive classes (row, col-*, container, etc.).
- Do not add unnecessary new HTML elements; prefer attributes on existing elements.

FULL CURRENT TEMPLATE:
```html
{template_content}
```

Return ONLY the full corrected template, no explanations."""

    return prompt.strip()


def fix_templates_with_axe_violations(
    issues_by_template: Dict[str, List[Dict]], project_root: Path, client
) -> Dict[str, Dict[str, str]]:
    """
    Use the Axe information already mapped to each template to ask the LLM to
    fix the full HTML of each *.component.html.

    Returns a dict:
      { template_rel_path: { "original": ..., "corrected": ... }, ... }
    """
    import re
    fixes: Dict[str, Dict[str, str]] = {}

    if not issues_by_template:
        print("[Angular + Axe] No violations mapped to templates.")
        return fixes

    for rel_path, issues in issues_by_template.items():
        try:
            # Support both HTML file templates and INLINE templates in .ts
            ts_inline_suffix = "::inline_template_"
            is_inline = ts_inline_suffix in rel_path

            if is_inline:
                # Example rel_path:
                #   "src/app/components/ng-style/ng-style.component.ts::inline_template_1"
                ts_rel, inline_id = rel_path.split(ts_inline_suffix, 1)
                tpl_path = project_root / ts_rel
                if not tpl_path.exists():
                    continue
                ts_content = tpl_path.read_text(encoding="utf-8")

                # Relocate all template: ` ... ` occurrences
                inline_matches = list(
                    re.finditer(
                        r"template\s*:\s*`([\s\S]*?)`",
                        ts_content,
                        flags=re.MULTILINE,
                    )
                )
                if not inline_matches:
                    continue

                # Compute inline template index (1-based in virtual name)
                try:
                    target_idx = int(inline_id)
                except ValueError:
                    target_idx = 1

                if target_idx < 1 or target_idx > len(inline_matches):
                    continue

                match = inline_matches[target_idx - 1]
                original_content = match.group(1)
            else:
                tpl_path = project_root / rel_path
                if not tpl_path.exists():
                    continue

                original_content = tpl_path.read_text(encoding="utf-8")

            if not original_content.strip():
                continue

            # CRITICAL VALIDATION: ensure violations actually belong to this template
            print(f"[Angular + Axe] 🔍 Validating violation mapping for {rel_path}...")
            valid_issues = []
            invalid_issues = []
            
            for issue in issues:
                violation = issue.get("violation", {})
                node = issue.get("node", {})
                html_snippet = (node.get("html") or "").strip()
                violation_id = violation.get("id", "unknown")
                is_valid = True
                
                if html_snippet:
                    # Extract snippet's main tag
                    snippet_tag_match = re.search(r'<(\w+)', html_snippet)
                    if snippet_tag_match:
                        snippet_tag = snippet_tag_match.group(1)
                        # Ensure the tag is in the template
                        if snippet_tag not in ['html', 'body', 'head']:  # Exclude root tags
                            if f'<{snippet_tag}' not in original_content and f'<{snippet_tag} ' not in original_content:
                                print(f"[Angular + Axe] ⚠️ Violation {violation_id} has element <{snippet_tag}> not in this template")
                                print(f"  → HTML snippet: {html_snippet[:150]}...")
                                print(f"  → This violation will be SKIPPED because mapping looks incorrect")
                                is_valid = False
                
                if is_valid:
                    valid_issues.append(issue)
                else:
                    invalid_issues.append(issue)
            
            if invalid_issues:
                print(f"[Angular + Axe] ⚠️ Skipped {len(invalid_issues)} violation(s) with incorrect mapping")
            
            if not valid_issues:
                print(f"[Angular + Axe] ⚠️ No valid violations to fix in {rel_path}. Skipping...")
                continue
            
            # Use only valid violations
            issues = valid_issues
            print(f"[Angular + Axe] ✓ {len(issues)} valid violation(s) to fix in {rel_path}")
            
            prompt = _build_axe_based_prompt_for_template(
                rel_path, original_content, issues
            )

            system_message = (
                "You are an EXPERT in web accessibility (WCAG 2.2 A+AA) and Angular. "
                "Your MISSION is to fix ALL accessibility violations reported by Axe "
                "by modifying the full HTML template. "
                "🚨 CRITICAL: You MUST make real changes to the code. Do NOT return the same code. "
                "🚨 If there are contrast violations, you MUST add or modify style=\"color: #XXXXXX;\" "
                "🚨 If there are aria-label, button-name, link-name violations, etc., you MUST add the required attributes. "
                "🚨 Keep Angular logic (bindings, *ngIf, *ngFor, pipes) intact. "
                "🚨 If you return the same code unchanged, the fix FAILS completely."
            )

            print(f"[Angular + Axe] Fixing template based on Axe: {rel_path}")
            
            # Log prompt for debugging (first 1000 chars)
            print(f"[Angular + Axe] 📝 Prompt (first 1000 chars): {prompt[:1000]}...")
            print(f"[Angular + Axe] 📄 Original code (first 500 chars): {original_content[:500]}...")

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )

            corrected = response.choices[0].message.content or ""
            
            # Log LLM response (first 500 chars)
            print(f"[Angular + Axe] 📝 LLM response (first 500 chars): {corrected[:500]}...")
            
            log_openai_call(
                prompt=prompt,
                response=corrected,
                model="gpt-4o",
                call_type="angular_axe_template_fix",
            )

            # Strip possible code block markers
            corrected = corrected.strip()
            if corrected.startswith("```"):
                parts = corrected.split("```")
                if len(parts) >= 3:
                    code_block = parts[1]
                    # Remove possible language tags
                    if "\n" in code_block:
                        code_block = code_block.split("\n", 1)[1]
                    corrected = code_block.strip()
                else:
                    corrected = corrected.replace("```html", "").replace("```", "").strip()

            # Apply automatic post-processing fixes
            corrected = _apply_automatic_accessibility_fixes(corrected)
            
            # Fix basic syntax errors
            corrected = _fix_basic_syntax_errors(corrected)
            
            # Fix Angular syntax for ARIA attributes
            corrected = _fix_angular_aria_syntax(corrected)

            # CRITICAL VALIDATION: ensure LLM returned valid HTML
            is_valid_response = True

            # 1. Must not be a comment or non-HTML text
            if corrected.strip().startswith("//") or corrected.strip().startswith("/*"):
                print(f"[Angular + Axe] ⚠️ LLM returned a comment instead of HTML for {rel_path}")
                is_valid_response = False
            
            # 2. Must contain at least one HTML tag
            if is_valid_response and not re.search(r'<\w+', corrected):
                print(f"[Angular + Axe] ⚠️ LLM did not return valid HTML for {rel_path}")
                is_valid_response = False
            
            # 3. Must not be significantly shorter than original (>50% shorter)
            if is_valid_response and len(corrected.strip()) < len(original_content.strip()) * 0.5:
                print(f"[Angular + Axe] ⚠️ LLM response too short for {rel_path} ({len(corrected)} vs {len(original_content)} chars)")
                is_valid_response = False

            # Detect differences more robustly (including color changes)
            orig_colors = re.findall(r'color\s*:\s*["\']?([^"\';]+)', original_content, re.IGNORECASE)
            corr_colors = re.findall(r'color\s*:\s*["\']?([^"\';]+)', corrected, re.IGNORECASE) if corrected else []
            has_color_diff = set(orig_colors) != set(corr_colors)
            
            # More robust comparison: normalise spaces but detect real changes
            orig_normalized = re.sub(r'\s+', ' ', original_content.strip())
            corr_normalized = re.sub(r'\s+', ' ', corrected.strip()) if corrected else ""
            
            # Detect changes in ARIA attributes, alt, aria-label, etc.
            orig_aria = set(re.findall(r'aria-\w+="[^"]*"', original_content, re.IGNORECASE))
            corr_aria = set(re.findall(r'aria-\w+="[^"]*"', corrected, re.IGNORECASE)) if corrected else set()
            has_aria_diff = orig_aria != corr_aria
            
            orig_alt = set(re.findall(r'alt="[^"]*"', original_content, re.IGNORECASE))
            corr_alt = set(re.findall(r'alt="[^"]*"', corrected, re.IGNORECASE)) if corrected else set()
            has_alt_diff = orig_alt != corr_alt
            
            orig_labels = set(re.findall(r'<label[^>]*>', original_content, re.IGNORECASE))
            corr_labels = set(re.findall(r'<label[^>]*>', corrected, re.IGNORECASE)) if corrected else set()
            has_label_diff = orig_labels != corr_labels
            
            has_changes = (
                orig_normalized != corr_normalized or 
                has_color_diff or
                has_aria_diff or
                has_alt_diff or
                has_label_diff or
                corrected.strip() != original_content.strip()
            )
            
            # Debug: show whether there are changes
            print(f"[Angular + Axe] 🔍 Change analysis:")
            print(f"  - Normalised code equal: {orig_normalized == corr_normalized}")
            print(f"  - Color diff: {has_color_diff} (orig: {orig_colors}, corr: {corr_colors})")
            print(f"  - ARIA diff: {has_aria_diff} (orig: {len(orig_aria)}, corr: {len(corr_aria)})")
            print(f"  - alt diff: {has_alt_diff} (orig: {len(orig_alt)}, corr: {len(corr_alt)})")
            print(f"  - labels diff: {has_label_diff} (orig: {len(orig_labels)}, corr: {len(corr_labels)})")
            print(f"  - Has changes: {has_changes}")
            
            if not has_changes:
                print(f"[Angular + Axe] ⚠️ NO CHANGES DETECTED - Detailed comparison:")
                print(f"  - Original (first 300): {original_content[:300]}")
                print(f"  - Corrected (first 300): {corrected[:300] if corrected else 'N/A'}")
                print(f"  - Original length: {len(original_content)}")
                print(f"  - Corrected length: {len(corrected) if corrected else 0}")
            
            if is_valid_response and corrected and has_changes:
                if has_color_diff:
                    print(f"[Angular + Axe] 🎨 Color difference detected: {orig_colors} -> {corr_colors}")
                if is_inline:
                    # Replace only the inline template content inside the .ts file
                    before = ts_content[: match.start(1)]
                    after = ts_content[match.end(1) :]

                    # Escape backticks inside the corrected template
                    safe_corrected = corrected.replace("`", "\\`")

                    new_ts_content = before + safe_corrected + after
                    if new_ts_content != ts_content:
                        try:
                            tpl_path.write_text(new_ts_content, encoding="utf-8")
                            # Verify write succeeded
                            written_content = tpl_path.read_text(encoding="utf-8")
                            if written_content.strip() == new_ts_content.strip():
                                fixes[rel_path] = {
                                    "original": original_content,
                                    "corrected": corrected,
                                }
                                print(
                                    f"[Angular + Axe] ✓ Changes applied and verified in inline template of {rel_path}"
                                )
                                print(f"  → Original length: {len(original_content)} chars")
                                print(f"  → Corrected length: {len(corrected)} chars")
                            else:
                                print(
                                    f"[Angular + Axe] ⚠️ Error: File was not written correctly in inline template of {rel_path}"
                                )
                        except Exception as e:
                            print(f"[Angular + Axe] ⚠️ Error writing file {rel_path}: {e}")
                    else:
                        print(
                            f"[Angular + Axe] ⚠️ No se aplicaron cambios efectivos en template inline de {rel_path}"
                        )
                        print(f"  → New content is identical to original")
                        print(f"  → Original (primeros 200): {original_content[:200]}")
                        print(f"  → Corregido (primeros 200): {corrected[:200]}")
                else:
                    # Verificar que el archivo existe y es escribible
                    if not tpl_path.exists():
                        print(f"[Angular + Axe] ⚠️ File {tpl_path} does not exist. Cannot apply changes.")
                        continue
                    
                    # Escribir el archivo
                    try:
                        tpl_path.write_text(corrected, encoding="utf-8")
                        # Verify write succeeded
                        written_content = tpl_path.read_text(encoding="utf-8")
                        if written_content.strip() == corrected.strip():
                            fixes[rel_path] = {
                                "original": original_content,
                                "corrected": corrected,
                            }
                            print(f"[Angular + Axe] ✓ Changes applied and verified in {rel_path}")
                            print(f"  → Original length: {len(original_content)} chars")
                            print(f"  → Corrected length: {len(corrected)} chars")
                        else:
                            print(f"[Angular + Axe] ⚠️ Error: File was not written correctly in {rel_path}")
                    except Exception as e:
                        print(f"[Angular + Axe] ⚠️ Error escribiendo archivo {rel_path}: {e}")
            else:
                print(f"[Angular + Axe] ⚠️ LLM returned the same code for {rel_path}")
                # Show which violations were attempted
                violation_ids = [issue.get("violation", {}).get("id", "unknown") for issue in issues]
                print(f"  → Violations that were attempted: {', '.join(set(violation_ids))}")
                print(f"  → Total violations: {len(issues)}")
                # Mostrar un ejemplo de HTML snippet para debugging
                if issues:
                    for i, issue in enumerate(issues[:3], 1):
                        violation = issue.get("violation", {})
                        node = issue.get("node", {})
                        html_snippet = (node.get("html") or "")[:200]
                        violation_id = violation.get("id", "unknown")
                        print(f"  → Violation {i} ({violation_id}): {html_snippet}...")
                
                # Show what should have been fixed
                print(f"[Angular + Axe] 💡 What should have been fixed:")
                for issue in issues:
                    violation = issue.get("violation", {})
                    violation_id = violation.get("id", "unknown")
                    if "button-name" in violation_id.lower():
                        print(f"  - Add aria-label or visible text to <button>")
                    elif "color-contrast" in violation_id.lower():
                        print(f"  - Add/modify style=\"color: #XXXXXX;\"")
                    elif "link-name" in violation_id.lower():
                        print(f"  - Add descriptive text or aria-label to <a>")
                    elif "aria" in violation_id.lower():
                        print(f"  - Add/modify aria-* attributes")
                    elif "alt" in violation_id.lower() or "image" in violation_id.lower():
                        print(f"  - Add/modify alt attribute on <img>")
                
                print(f"[Angular + Axe] ⚠️ LLM did not apply fixes. Possible reasons:")
                print(f"  1. Violation element is not in the template (wrong mapping)")
                print(f"  2. LLM did not find the correct element in the code")
                print(f"  3. Prompt was not specific enough")
                print(f"  4. LLM decided no changes needed (incorrect)")

        except Exception as e:
            print(f"[Angular + Axe] ⚠️ Error fixing {rel_path}: {e}")

    return fixes


def process_angular_project(project_path: str, client, run_path: str, serve_app: bool = False) -> List[str]:
    """
    Process a local Angular project: detect components and apply accessibility
    fixes using the LLM.

    Args:
        project_path: Absolute path to the Angular project.
        client: OpenAI client already initialised.
        run_path: Path where reports and artifacts will be saved.

    Returns:
        List of summary lines to display in the console.
    """
    project_root = Path(project_path).resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"Path {project_root} does not exist.")

    angular_config = project_root / ANGULAR_CONFIG_FILE
    if not angular_config.exists():
        raise ValueError("angular.json not found in project. Ensure you point to a valid Angular project.")

    config_data = _load_angular_config(angular_config)
    source_roots = _resolve_source_roots(project_root, config_data)

    if not source_roots:
        raise ValueError("Could not determine source directory from angular.json.")

    templates = _discover_component_templates(source_roots)

    summary_lines: List[str] = []
    stats = {"templates": len(templates), "updated": 0, "errors": 0, "build_failures": 0, "compilation_fixes": 0}
    processed_components: List[Dict] = []
    changes_map: List[Dict] = []  # Map of changes to apply later

    # PHASE 1: Compile project and capture compilation errors
    print("\n[Phase 1] Compiling Angular project...")
    build_result = _compile_and_get_errors(project_root)
    
    # Debug: mostrar errores si los hay
    if not build_result["success"] and build_result.get("errors"):
        print(f"  → Errors detected: {len(build_result.get('errors', []))}")
        for i, error in enumerate(build_result.get("errors", [])[:3], 1):
            print(f"    Error {i}: {error[:200]}...")
    
    if not build_result["verification_available"]:
        print("⚠️ Could not compile project (ng not available).")
        print("  Continuing with accessibility fixes...")
    elif build_result["success"]:
        print("✓ Project compiles successfully.")
    else:
        print(f"✗ Project has {len(build_result.get('errors', []))} compilation errors.")
        print("  Fixing compilation errors with LLM...")

        # Fix compilation errors with LLM
        compilation_fixes = _fix_compilation_errors(build_result.get("errors", []), project_root, client)
        stats["compilation_fixes"] = len(compilation_fixes)
        
        if compilation_fixes:
            print(f"  → Applying {len(compilation_fixes)} compilation fixes...")
            _apply_compilation_fixes(compilation_fixes, project_root)
            
            # Recompilar para verificar
            print("  → Recompiling after fixes...")
            build_result = _compile_and_get_errors(project_root)
            if build_result["success"]:
                print("  ✓ Compilation errors fixed successfully.")
            else:
                print(f"  ⚠️ Still {len(build_result.get('errors', []))} compilation errors remaining.")
                summary_lines.append(f"⚠️ {len(build_result.get('errors', []))} compilation errors pending")

    # FASE 2: Ejecutar Axe para obtener errores reales de accesibilidad
    print(f"\n[Phase 2] Running Axe analysis to detect real errors...")
    axe_results = None
    issues_by_template = {}
    dev_server_process = None
    screenshot_paths = []  # Inicializar lista de capturas
    
    if serve_app:
        try:
            import time
            import socket
            from urllib.request import urlopen
            from urllib.error import URLError
            
            base_url = "http://localhost:4200"
            
            # First check if server is already running
            server_running = False
            try:
                response = urlopen(base_url, timeout=2)
                server_running = True
                print(f"  → Angular server already running at {base_url}")
            except (URLError, socket.timeout):
                print(f"  → Angular server not running, starting it...")
                # Iniciar el servidor Angular antes de ejecutar Axe
                dev_server_process = _start_angular_dev_server(project_root, port=4200, wait_for_ready=True)
                if dev_server_process:
                    print(f"  → Waiting for server to be ready...")
                    # Wait until server is ready
                    max_wait = 120  # 2 minutes max
                    wait_interval = 2
                    waited = 0
                    while waited < max_wait:
                        try:
                            response = urlopen(base_url, timeout=2)
                            server_running = True
                            print(f"  ✓ Angular server ready at {base_url}")
                            break
                        except (URLError, socket.timeout):
                            time.sleep(wait_interval)
                            waited += wait_interval
                            print(f"  → Esperando... ({waited}s)")
                    
                    if not server_running:
                        print(f"  ⚠️ Could not connect to server after {max_wait}s")
                        print("  → Continuing with static code analysis...")
            
            # Run Axe if server is running
            if server_running:
                print("  → Running Axe on Angular application...")
                try:
                    driver = setup_driver()
                    driver.get(base_url)
                    time.sleep(5)  # Wait for page to load fully
                    
                    # Take screenshots automatically (before fixes)
                    print("  → Taking screenshots at different sizes...")
                    screenshots_dir = Path(run_path) / "screenshots" / "before"
                    screenshot_paths = take_screenshots(
                        driver,
                        base_url,
                        screenshots_dir,
                        prefix="before"
                    )
                    if screenshot_paths:
                        print(f"  ✓ {len(screenshot_paths)} capturas guardadas")
                        # Crear resumen HTML de las capturas
                        summary_path = screenshots_dir / "summary.html"
                        create_screenshot_summary(screenshot_paths, summary_path)
                        print(f"  ✓ Resumen visual guardado en: {summary_path}")
                        print(f"  → Screenshots will be included in the LLM prompt for better visual context")
                    else:
                        screenshot_paths = []  # Ensure it's an empty list
                    
                    # Run Axe analysis
                    axe_results = run_axe_analysis(driver, base_url, is_local_file=False)
                    driver.quit()
                    
                    # Guardar las rutas de capturas para usarlas en el procesamiento de componentes
                    # (will be stored in a variable to pass to components)
                    
                    if axe_results and axe_results.get("violations"):
                        print(f"  ✓ Axe reported {len(axe_results['violations'])} violations")
                        issues_by_template = map_axe_violations_to_templates(axe_results, project_root, source_roots)
                        print(f"  ✓ Errores mapeados a {len(issues_by_template)} templates")
                        
                        # Guardar reporte de Axe en el directorio de resultados
                        axe_report_path = Path(run_path) / "angular_axe_report.json"
                        with open(axe_report_path, "w", encoding="utf-8") as f:
                            json.dump(axe_results, f, indent=2, ensure_ascii=False)
                        print(f"  ✓ Reporte de Axe guardado en: {axe_report_path}")
                    else:
                        print("  ⚠️ Axe reported no violations (may be no errors or page did not load)")
                except Exception as e:
                    print(f"  ⚠️ No se pudo ejecutar Axe: {e}")
                    print("  → Continuing with static code analysis...")
        except Exception as e:
            print(f"  ⚠️ Error al intentar ejecutar Axe: {e}")
            print("  → Continuing with static code analysis...")
    else:
        print("  → No-server mode: using static code analysis only")
    
    # FASE 3: Procesar componentes y generar mapa de cambios de accesibilidad (sandbox)
    print(f"\n[Fase 3] Generando mapa de cambios de accesibilidad en sandbox...")
    for template_path in templates:
        try:
            # Get Axe errors for this specific template
            template_rel_path = str(template_path.relative_to(project_root))
            axe_errors_for_template = issues_by_template.get(template_rel_path, [])
            
            # Get screenshot paths if available
            screenshot_paths_for_component = []
            if screenshot_paths:
                # Por ahora, pasamos todas las capturas a cada componente
                # Could filter by component in the future if needed
                screenshot_paths_for_component = screenshot_paths
            
            component_result, changes = _process_single_component_sandbox(
                template_path, client, project_root, axe_errors_for_template, screenshot_paths_for_component
            )
            processed_components.append(component_result)
            if changes:
                changes_map.append({
                    "component": component_result["component_name"],
                    "template_path": str(template_path),
                    "changes": changes
                })
            if component_result["status"] == "updated":
                stats["updated"] += 1
            relative_template = Path(component_result["template_path"]).relative_to(project_root)
            summary_lines.append(f"✓ {relative_template} -> {component_result['status']}")
        except Exception as exc:
            stats["errors"] += 1
            relative_path = template_path.relative_to(project_root)
            error_msg = f"✗ {relative_path} - Error: {exc}"
            summary_lines.append(error_msg)
            processed_components.append(
                {
                    "component_name": template_path.stem.replace(".component", ""),
                    "template_path": str(relative_path),
                    "status": "error",
                    "error": str(exc),
                }
            )

    # PHASE 4: Apply accessibility changes to actual source code
    print(f"\n[Phase 4] Applying {len(changes_map)} accessibility changes to source code...")
    applied_changes = _apply_changes_map(changes_map, project_root)
    
    # Verify final build after applying accessibility changes
    print(f"\n[Phase 5] Verifying final build...")
    final_build_result = _compile_and_get_errors(project_root)
    
    if not final_build_result["verification_available"]:
        print("⚠️ Could not verify final build (ng not available).")
        summary_lines.append("⚠️ Changes applied but could not verify final build")
    elif not final_build_result["success"]:
        stats["build_failures"] = 1
        print(f"✗ ERROR: Project does not compile after applying changes ({len(final_build_result.get('errors', []))} errors).")
        print("  ⚠️ Changes are kept so you can fix them manually.")
        summary_lines.append(f"⚠️ Changes applied but {len(final_build_result.get('errors', []))} compilation errors remain")
    else:
        print("✓ Project compiles successfully after all fixes.")
        summary_lines.append(f"✓ Build verified: {applied_changes} changes applied successfully")
    
    # Note: If serve_app=True, server was already started in Phase 2 (before running Axe)
    # Solo mostramos un mensaje informativo si el servidor sigue corriendo
    if serve_app and dev_server_process:
        print(f"\n[Info] Angular server is running at http://localhost:4200")
        print(f"  → Server will keep running. Press Ctrl+C in the terminal where it was started to stop it.")
    elif serve_app:
        print(f"\n[Info] If the Angular server is not running, you can start it manually with: ng serve")

    report_payload = {
        "project_root": str(project_root),
        "stats": stats,
        "components": processed_components,
        "changes_map": changes_map,
        "build_verification": {
            "initial_build": build_result.get("success", False),
            "initial_verification_available": build_result.get("verification_available", False),
            "final_build": final_build_result.get("success", False),
            "final_verification_available": final_build_result.get("verification_available", False),
            "compilation_errors_fixed": stats["compilation_fixes"],
        },
    }

    report_path = Path(run_path) / "angular_summary.json"
    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump(report_payload, report_file, indent=2, ensure_ascii=False)

    headline = f"Componentes encontrados: {stats['templates']} | Actualizados: {stats['updated']} | Errores: {stats['errors']}"
    return [headline, "-" * len(headline), *summary_lines, f"Resumen guardado en {report_path}"]


def _load_angular_config(config_path: Path) -> Dict:
    with open(config_path, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def _get_default_project_name(project_root: Path) -> Optional[str]:
    """
    Obtiene el nombre del proyecto por defecto para workspaces multi-proyecto.
    Retorna None si es un proyecto simple o no se puede determinar.
    """
    angular_config = project_root / ANGULAR_CONFIG_FILE
    if not angular_config.exists():
        return None
    
    try:
        config = _load_angular_config(angular_config)
        projects = config.get("projects", {})
        
        # Si solo hay un proyecto, no hace falta especificarlo
        if len(projects) <= 1:
            return None
        
        # Buscar proyecto por defecto
        default_project = config.get("defaultProject")
        if default_project and default_project in projects:
            return default_project
        
        # Si no hay defaultProject, buscar el primer proyecto con architect.build
        for name, proj_config in projects.items():
            architect = proj_config.get("architect", {})
            if "build" in architect:
                return name
        
        # Fallback: primer proyecto
        return list(projects.keys())[0] if projects else None
    except Exception:
        return None


def _resolve_source_roots(project_root: Path, config: Dict) -> List[Path]:
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

    # fallback: typical src/ directory
    fallback_src = project_root / "src"
    if not source_roots and fallback_src.exists():
        source_roots.append(fallback_src)

    return source_roots


def _discover_component_templates(source_roots: List[Path]) -> List[Path]:
    templates: List[Path] = []
    for root in source_roots:
        templates.extend(root.glob("**/*.component.html"))
    return sorted(templates)


def _process_single_component_sandbox(
    template_path: Path, client, project_root: Path, axe_errors: List[Dict] = None, screenshot_paths: List[str] = None
) -> Tuple[Dict, Optional[Dict]]:
    """
    Process a component in sandbox mode, producing a change map without modifying source code.

    Args:
        template_path: Path to the component template
        client: OpenAI client
        project_root: Project root path
        axe_errors: List of Axe errors mapped to this template (optional)

    Returns:
        Tuple of (component result, change map)
    """
    base_component_name = template_path.stem.replace(".component", "")
    component_dir = template_path.parent

    ts_path = component_dir / (template_path.name.replace(".html", ".ts"))
    styles_candidates = [
        component_dir / (template_path.name.replace(".html", ".scss")),
        component_dir / (template_path.name.replace(".html", ".sass")),
        component_dir / (template_path.name.replace(".html", ".css")),
    ]
    style_path = next((path for path in styles_candidates if path.exists()), None)

    template_content = template_path.read_text(encoding="utf-8")
    ts_content = ts_path.read_text(encoding="utf-8") if ts_path.exists() else None
    style_content = style_path.read_text(encoding="utf-8") if style_path and style_path.exists() else None

    # Analyse template for obvious errors before sending to LLM
    detected_errors = _analyze_template_for_accessibility_errors(template_content, style_content)
    
    # Convert Axe errors to a readable format for the prompt
    axe_errors_formatted = []
    if axe_errors:
        import re
        print(f"  → {len(axe_errors)} Axe errors detected for this component")
        for axe_error in axe_errors:
            # Extract info from Axe structure
            violation = axe_error.get("violation", {})
            node = axe_error.get("node", {})
            violation_id = axe_error.get("violation_id", violation.get("id", "unknown"))
            
            # Node CSS selector
            targets = node.get("target", [])
            selector = targets[0] if targets and isinstance(targets[0], str) else "No selector"
            
            # Affected node HTML
            html_snippet = (node.get("html") or "").strip()
            html_display = html_snippet[:200] if html_snippet else ""  # First 200 chars

            # Violation description
            description = violation.get("description", "")
            help_text = violation.get("help", "")
            
            # Contrast-specific data (if applicable)
            contrast_info = ""
            if violation_id == "color-contrast":
                # Look for contrast data in Axe checks
                all_checks = node.get("all", []) or []
                any_checks = node.get("any", []) or []
                checks = all_checks + any_checks
                
                for check in checks:
                    check_data = check.get("data", {})
                    bg_color = check_data.get("bgColor", "")
                    fg_color = check_data.get("fgColor", "")
                    ratio = check_data.get("contrastRatio", "")
                    expected_ratio = check_data.get("expectedContrastRatio", "")
                    
                    if bg_color or fg_color or ratio:
                        contrast_info = f" | Text color: {fg_color}, Background color: {bg_color}, Actual ratio: {ratio}, Required ratio: {expected_ratio}"
                        break
                
                # If not found in all/any, look in failureSummary or check message
                if not contrast_info:
                    failure_summary = node.get("failureSummary", "")
                    if failure_summary:
                        import re
                        # Extract ratio from error message (format: "contrast of 3.33")
                        ratio_match = re.search(r'contrast of ([\d.]+)', failure_summary, re.IGNORECASE)
                        expected_match = re.search(r'Expected contrast ratio of ([\d.]+:?[\d]*)', failure_summary, re.IGNORECASE)
                        fg_match = re.search(r'foreground color: (#[0-9a-fA-F]+)', failure_summary, re.IGNORECASE)
                        bg_match = re.search(r'background color: (#[0-9a-fA-F]+)', failure_summary, re.IGNORECASE)
                        
                        if ratio_match or expected_match:
                            ratio_str = ratio_match.group(1) if ratio_match else "N/A"
                            expected_str = expected_match.group(1) if expected_match else "4.5:1"
                            fg_str = fg_match.group(1) if fg_match else "N/A"
                            bg_str = bg_match.group(1) if bg_match else "N/A"
                            contrast_info = f" | Text color: {fg_str}, Background color: {bg_str}, Actual ratio: {ratio_str}, Required ratio: {expected_str}"

                    # If we still have no info, search in check messages
                    if not contrast_info:
                        for check in checks:
                            message = check.get("message", "")
                            if "contrast" in message.lower() and ("insufficient" in message.lower() or "ratio" in message.lower()):
                                import re
                                ratio_match = re.search(r'contrast of ([\d.]+)', message, re.IGNORECASE)
                                expected_match = re.search(r'Expected contrast ratio of ([\d.]+:?[\d]*)', message, re.IGNORECASE)
                                fg_match = re.search(r'foreground color: (#[0-9a-fA-F]+)', message, re.IGNORECASE)
                                bg_match = re.search(r'background color: (#[0-9a-fA-F]+)', message, re.IGNORECASE)
                                
                                if ratio_match:
                                    ratio_str = ratio_match.group(1)
                                    expected_str = expected_match.group(1) if expected_match else "4.5:1"
                                    fg_str = fg_match.group(1) if fg_match else "N/A"
                                    bg_str = bg_match.group(1) if bg_match else "N/A"
                                    contrast_info = f" | Text color: {fg_str}, Background color: {bg_str}, Actual ratio: {ratio_str}, Required ratio: {expected_str}"
                                    break
            
            # Format Axe error in a very specific and detailed way
            error_parts = [f"ERROR AXE: {violation_id}"]
            
            if selector and selector != "No selector":
                error_parts.append(f"Selector CSS: {selector}")
                
                # Advertir si el selector apunta a un elemento generado por Angular Material
                if ".mdc-button__label" in selector or ".mat-button-label" in selector or " > " in selector:
                    # Extraer el selector del padre (antes de " > ")
                    parent_selector = selector.split(" > ")[0] if " > " in selector else selector.replace(".mdc-button__label", "").strip()
                    error_parts.append(f"⚠️ NOTE: This selector targets an internal element generated by Angular Material. Find the PARENT element in the template (e.g. button with {parent_selector}) and apply the style there.")
            
            if description:
                error_parts.append(f"Description: {description}")
            
            if contrast_info:
                error_parts.append(f"Datos contraste: {contrast_info.strip()}")
            
            if html_display:
                # Strip Angular runtime attributes for display
                clean_html = re.sub(r'\s+_ngcontent-[^=]*="[^"]*"', '', html_display)
                clean_html = re.sub(r'\s+_nghost-[^=]*="[^"]*"', '', clean_html)
                error_parts.append(f"HTML afectado: {clean_html}")
                
                # Si el HTML es un span con clase mdc-button__label, advertir que es generado
                if "mdc-button__label" in clean_html or "mat-button-label" in clean_html:
                    # Try to extract button text to help locate it
                    text_match = re.search(r'>\s*([^<]+)\s*<', clean_html)
                    if text_match:
                        button_text = text_match.group(1).strip()
                        error_parts.append(f"⚠️ NOTE: This span is generated by Angular Material. Find the button that contains the text '{button_text}' in the template.")
            
            if help_text:
                error_parts.append(f"Ayuda: {help_text}")
            
            error_msg = " | ".join(error_parts)
            
            axe_errors_formatted.append(error_msg)
            detected_errors.append(error_msg)  # Also add to detected_errors so they are included in the prompt
    
    if detected_errors:
        print(f"  → Total de {len(detected_errors)} errores de accesibilidad detectados en {base_component_name}")
        for error in detected_errors[:5]:
            print(f"    - {error[:80]}")
    else:
        print(f"  → No obvious errors detected in {base_component_name} (LLM should look deeper)")

    system_message = (
        "You are an EXPERT WEB ACCESSIBILITY AUDITOR and Angular developer. Your CRITICAL MISSION is: "
        "1) THOROUGHLY ANALYSE every line of code to find ALL accessibility errors (WCAG 2.2 A+AA), "
        "2) FIX EVERY ERROR found WITHOUT EXCEPTION, even if it requires significant changes. "
        "You MUST ACTIVELY LOOK FOR: buttons/links without visible text or aria-label, inputs without labels, images without alt, "
        "contrast issues, missing keyboard support, incorrect heading hierarchy, lists without structure, etc. "
        "🚨🚨🚨 CRITICAL ON CONTRAST: If contrast errors are detected or you find elements with text that may have low contrast, "
        "you MUST fix ALL contrast errors by adjusting text and/or background colour to meet WCAG (4.5:1 for normal text, 3:1 for large text). "
        "On light backgrounds, use dark text colour (#000000, #212121, etc.); on dark backgrounds, use light text (#FFFFFF, #F5F5F5, etc.). "
        "Do NOT fix just one, fix ALL. If there are 3 contrast errors, fix all 3. "
        "🚨🚨🚨 CRITICAL ON RESPONSIVE DESIGN: "
        "- PRESERVE ALL existing responsive styles (media queries, responsive classes, flexbox, grid, etc.) "
        "- Do NOT change display:none to display:block unless absolutely necessary for accessibility "
        "- If a label has display:none, it is visually hidden but accessible to screen readers - use sr-only or aria-label instead "
        "- Do NOT add inline styles that break responsive design (fixed width, fixed height, excessive margin/padding, etc.) "
        "- Keep all Bootstrap/CSS framework classes (col-sm-*, col-md-*, etc.) "
        "- Do NOT modify layout properties like display, position, flex, grid, width, height, margin, padding unless critical for accessibility "
        "🚨🚨🚨 CRITICAL ON SCREENSHOTS (if provided): "
        "If screenshots are provided in the user message, you MUST examine them in detail. "
        "These screenshots show how the application REALLY looks at different screen sizes. "
        "YOUR GOAL: Fix ALL accessibility errors BUT preserve EXACTLY the visual design you see in the screenshots. "
        "Fixes should be visually 'invisible' - use aria-label, roles, alt text, and minimal contrast adjustments. "
        "The final result must look IDENTICAL to the screenshots, but accessible. "
        "IMPORTANT: If the code has ANY accessibility issue, you MUST fix it. "
        "Do NOT return the original code unchanged. ALWAYS look for and fix errors. "
        "Accessibility IS IMPORTANT AND MUST BE FIXED, BUT if screenshots are provided, preserve the visual design they show. "
        "Do NOT add HTML comments or attributes that show they were fixes. The code should look like original code."
    )

    # Contar errores de contraste detectados
    contrast_errors = [e for e in detected_errors if 'contraste' in e.lower() or 'contrast' in e.lower()]
    if contrast_errors:
        print(f"  → {len(contrast_errors)} contrast errors detected - LLM MUST fix ALL")

    user_prompt = _build_component_prompt(
        component_name=base_component_name,
        template_content=template_content,
        ts_content=ts_content,
        style_content=style_content,
        template_path=str(template_path),
        ts_path=str(ts_path) if ts_path.exists() else None,
        style_path=str(style_path) if style_path else None,
        detected_errors=detected_errors,
        contrast_errors_count=len(contrast_errors),
    )

    # Prepare messages, including screenshots if available
    messages = [
        {"role": "system", "content": system_message},
    ]
    
    # If screenshots are available, include them in the user message
    if screenshot_paths:
        import base64
        screenshot_instructions = """
📸 SCREENSHOTS - CRITICAL FOR PRESERVING DESIGN:

I have taken screenshots of the application at different screen sizes (mobile, tablet, desktop) that show how the page REALLY looks before the fixes.

🚨 MANDATORY INSTRUCTIONS ABOUT THE SCREENSHOTS:
1. EXAMINE each screenshot in detail to understand:
   - The current visual design (layout, colours, spacing, distribution)
   - How content adapts at different screen sizes
   - Which elements are visible/hidden at each size
   - The application's overall visual style

2. FIX ALL accessibility errors listed above, BUT:
   - KEEP the visual design you see in the screenshots
   - Do NOT change background colours, element sizes, or distribution shown in the images
   - For contrast errors: adjust ONLY the text colour, keeping the background visible in the screenshots
   - Do NOT add new visible elements (use aria-label or sr-only instead)
   - Do NOT change display:none to display:block if that element is not visible in the screenshots
   - Respect the responsive design: if it looks a certain way on mobile, keep it that way

3. YOUR GOAL: Fix ALL accessibility errors WITHOUT changing how the page looks in the screenshots.
   - Fixes should be visually "invisible"
   - Use aria-label, roles, alt text, and minimal contrast adjustments
   - The final design must look IDENTICAL to the screenshots, but accessible

The screenshots show the application BEFORE the fixes. Your job is to make it accessible while keeping that exact visual appearance.
"""
        user_content = [
            {"type": "text", "text": user_prompt + screenshot_instructions}
        ]
        # Add each screenshot as image
        for screenshot_path in screenshot_paths:
            try:
                screenshot_file = Path(screenshot_path)
                if screenshot_file.exists():
                    # Read and encode image as base64
                    with open(screenshot_file, "rb") as img_file:
                        image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
                        # Determine MIME type from extension
                        mime_type = "image/png"  # Por defecto PNG
                        if screenshot_path.endswith('.jpg') or screenshot_path.endswith('.jpeg'):
                            mime_type = "image/jpeg"
                        user_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            }
                        })
            except Exception as e:
                print(f"  ⚠️ Error al incluir captura {screenshot_path}: {e}")
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": user_prompt})

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.0,
    )

    response_text = response.choices[0].message.content or ""
    log_openai_call(prompt=user_prompt, response=response_text, model="gpt-4o", call_type="angular_component_fix")

    print(f"  → LLM responded with {len(response_text)} characters")
    
    # Debug: show first characters of response to see what is being returned
    print(f"  → Primeros 200 caracteres de respuesta: {response_text[:200]}")
    
    try:
        parsed_response = _parse_component_response(response_text)
        print(f"  → Respuesta parseada correctamente")
    except Exception as e:
        print(f"  ✗ Error parseando respuesta del LLM: {e}")
        print(f"  → Primeros 500 caracteres de la respuesta: {response_text[:500]}")
        # Intentar extraer template directamente si el parsing falla
        import re
        template_match = re.search(r'<<<TEMPLATE>>>\s*(.*?)\s*<<<END TEMPLATE>>>', response_text, re.DOTALL)
        if template_match:
            parsed_response = {"template": template_match.group(1).strip(), "typescript": None, "styles": None}
            print(f"  → Template extracted using alternative regex")
        else:
            print(f"  ✗ No se pudo extraer template de ninguna forma")
            return {
                "component_name": base_component_name,
                "template_path": str(template_path),
                "typescript_path": str(ts_path) if ts_path.exists() else None,
                "styles_path": str(style_path) if style_path else None,
                "status": "error",
                "error": f"Error parseando respuesta: {e}",
                "changes": {}
            }, None
    
    # Corregir sintaxis Angular para atributos ARIA con binding
    template_content_corrected = _fix_angular_aria_syntax(parsed_response.get("template"))
    
    # Fix common basic syntax errors (unclosed quotes, unclosed tags, etc.)
    template_content_corrected = _fix_basic_syntax_errors(template_content_corrected)
    
    # Apply automatic accessibility fixes (role="img" on icons, lang on html, etc.)
    template_content_corrected = _apply_automatic_accessibility_fixes(template_content_corrected)
    
    # Validar y corregir cambios que rompan el responsive
    template_content_corrected = _fix_responsive_breaking_changes(template_content, template_content_corrected)
    
    if not template_content_corrected:
        print(f"  ⚠️ No se obtuvo template corregido del LLM")
        return {
            "component_name": base_component_name,
            "template_path": str(template_path),
            "typescript_path": str(ts_path) if ts_path.exists() else None,
            "styles_path": str(style_path) if style_path else None,
            "status": "error",
            "error": "No se pudo obtener template corregido",
            "changes": {}
        }, None
    
    # Apply automatic fixes for detected contrast errors
    # IMPORTANT: these automatic fixes are disabled by default
    # porque pueden elegir un color incorrecto cuando el fondo real es oscuro.
    # Preferimos que el LLM (con el contexto completo) y/o el desarrollador
    # adjust contrast explicitly.
    contrast_errors = [e for e in detected_errors if 'contraste' in e.lower() or 'contrast' in e.lower()]
    if contrast_errors and ENABLE_AUTOMATIC_CONTRAST_FIXES:
        print(f"  → Applying automatic fixes for {len(contrast_errors)} detected contrast errors")
        template_content_corrected = _apply_automatic_contrast_fixes(template_content_corrected, contrast_errors)
    
    print(f"  → Template corregido: {len(template_content_corrected)} caracteres (original: {len(template_content)} caracteres)")
    
    # More robust comparison - normalise spaces but keep structure
    original_clean = '\n'.join(line.rstrip() for line in template_content.split('\n'))
    corrected_clean = '\n'.join(line.rstrip() for line in template_content_corrected.split('\n'))
    
    # Build change map without applying yet (sandbox)
    changes = {}
    
    # Compare in multiple ways
    are_different = (
        original_clean.strip() != corrected_clean.strip() or
        len(original_clean.strip()) != len(corrected_clean.strip()) or
        template_content.strip() != template_content_corrected.strip()
    )
    
    # If there are automatically detected errors, force changes to be considered
    # even when the comparison does not detect them (LLM may have made subtle changes)
    if detected_errors and not are_different:
        print(f"  ⚠️ No differences detected in comparison, but there are {len(detected_errors)} automatically detected errors")
        print(f"  → Forcing application of changes because there are errors that must be fixed")
        are_different = True
    
    # Debug: show specific differences when none are detected
    if not are_different:
        print(f"  ⚠️ Corrected template appears IDENTICAL to original")
        print(f"  → Comparing lines...")
        original_lines = template_content.strip().split('\n')
        corrected_lines = template_content_corrected.strip().split('\n')
        if len(original_lines) != len(corrected_lines):
            print(f"    → Different line count: {len(original_lines)} vs {len(corrected_lines)}")
            are_different = True
        else:
            print(f"    → Same line count: {len(original_lines)}")
            # Look for line-by-line differences
            differences_found = False
            for i, (orig, corr) in enumerate(zip(original_lines, corrected_lines)):
                if orig.strip() != corr.strip():
                    print(f"    → Difference at line {i+1}:")
                    print(f"      Original: {orig[:100]}")
                    print(f"      Corrected: {corr[:100]}")
                    differences_found = True
                    are_different = True
                    break
            if not differences_found:
                print(f"    → No line-by-line differences found")
                # If errors were detected, force changes anyway
                if detected_errors:
                    print(f"    → BUT there are {len(detected_errors)} detected errors, forcing application of changes")
                    are_different = True
    
    if are_different:
        print(f"  ✓ Changes detected in template of {base_component_name}")
        print(f"    → Original: {len(original_clean.strip())} chars, Corrected: {len(corrected_clean.strip())} chars")
        changes["template"] = {
            "path": str(template_path),
            "original": template_content,
            "corrected": template_content_corrected
        }
    else:
        print(f"  ⚠️ No changes detected in template of {base_component_name}")
        print(f"    → LLM returned the same code. This indicates that:")
        print(f"      1. The LLM did not detect accessibility errors")
        print(f"      2. The LLM detected errors but did not fix them")
        print(f"      3. The template really has no errors (unlikely)")
        
        # Show automatically detected errors if any
        if detected_errors:
            print(f"    → {len(detected_errors)} errors were detected automatically, but the LLM did not fix them")
            for error in detected_errors[:5]:
                print(f"      - {error[:80]}")
            # Force changes if errors were detected
            print(f"    → FORCING application of changes because errors were detected")
            changes["template"] = {
                "path": str(template_path),
                "original": template_content,
                "corrected": template_content_corrected
            }
    
    if ts_content is not None:
        ts_corrected = parsed_response.get("typescript")
        if ts_corrected and ts_corrected.strip() != ts_content.strip():
            changes["typescript"] = {
                "path": str(ts_path),
                "original": ts_content,
                "corrected": ts_corrected
            }
    
    if style_path and style_content is not None:
        style_corrected = parsed_response.get("styles")
        if style_corrected and style_corrected.strip() != style_content.strip():
            changes["styles"] = {
                "path": str(style_path),
                "original": style_content,
                "corrected": style_corrected
            }

    status = "updated" if changes else "unchanged"

    result = {
        "component_name": base_component_name,
        "template_path": str(template_path),
        "typescript_path": str(ts_path) if ts_path.exists() else None,
        "styles_path": str(style_path) if style_path else None,
        "status": status,
        "changes": {
            "template": "template" in changes,
            "typescript": "typescript" in changes,
            "styles": "styles" in changes,
        },
    }
    
    return result, changes if changes else None


def _categorize_errors(detected_errors: List[str]) -> Dict[str, List[str]]:
    """Agrupa los errores detectados por tipo"""
    categories = {
        "missing_alt": [],
        "missing_label": [],
        "missing_aria_label": [],
        "contrast": [],
        "other": []
    }
    
    for error in detected_errors:
        error_lower = error.lower()
        if "imagen sin alt" in error_lower or "sin alt" in error_lower or "without alt" in error_lower:
            categories["missing_alt"].append(error)
        elif "sin label" in error_lower or "input sin" in error_lower or "without label" in error_lower:
            categories["missing_label"].append(error)
        elif "button without" in error_lower or "link without" in error_lower or "aria-label" in error_lower:
            categories["missing_aria_label"].append(error)
        elif "contraste" in error_lower or "contrast" in error_lower:
            categories["contrast"].append(error)
        else:
            categories["other"].append(error)
    
    return categories


def _build_error_specific_prompt(error_type: str, errors: List[str]) -> str:
    """Build a specific, concise prompt for an error type"""
    if not errors:
        return ""
    
    if error_type == "missing_alt":
        return f"""🔴 IMAGES WITHOUT ALT ERRORS ({len(errors)} found):
{chr(10).join(f"- {e}" for e in errors)}

ACTION REQUIRED: Add the alt attribute to ALL mentioned images.
- If the image is informative: alt="Image description"
- If the image is decorative: alt=""
- In Angular, use [alt] for dynamic binding or alt="fixed text" for static

FIX ALL images listed above."""
    
    elif error_type == "missing_label":
        return f"""🔴 INPUTS WITHOUT LABEL ERRORS ({len(errors)} found):
{chr(10).join(f"- {e}" for e in errors)}

ACTION REQUIRED: Add <label> associated with ALL mentioned inputs.
IMPORTANT ON RESPONSIVE:
- If the input already has a label with display:none, do NOT change it to display:block
- Instead, add aria-label to the input: <input id="inputId" aria-label="Description" ... />
- Or use an sr-only (screen-reader-only) class for the label: <label for="inputId" class="sr-only">Text</label>
- Only change display if the label does NOT exist and needs to be visible

Correct example (preserving responsive):
  <label for="inputId" class="sr-only">Label text</label>
  <input id="inputId" ... />
  
Or alternatively:
  <input id="inputId" aria-label="Label text" ... />

FIX ALL inputs listed above."""
    
    elif error_type == "missing_aria_label":
        return f"""🔴 BUTTONS/LINKS WITHOUT ARIA-LABEL ERRORS ({len(errors)} found):
{chr(10).join(f"- {e}" for e in errors)}

ACTION REQUIRED: Add descriptive aria-label to ALL mentioned buttons/links.
- For static values: aria-label="Description"
- For dynamic binding in Angular: [attr.aria-label]="variable"

FIX ALL elements listed above."""
    
    elif error_type == "contrast":
        return f"""🔴 CONTRAST ERRORS ({len(errors)} found):
{chr(10).join(f"- {e}" for e in errors)}

ACTION REQUIRED: Fix colour contrast for ALL mentioned elements.
- Minimum ratio required: 4.5:1 for normal text, 3:1 for large text
- On light backgrounds: use style="color: #000000" or #212121
- On dark backgrounds: use style="color: #FFFFFF" or #F5F5F5
- Find ALL similar elements and fix them too

FIX ALL low-contrast elements listed above."""
    
    else:
        return f"""🔴 OTHER ERRORS ({len(errors)} found):
{chr(10).join(f"- {e}" for e in errors)}

ACTION REQUIRED: Fix these accessibility errors."""
    
    return ""


def _format_detected_errors(detected_errors: List[str]) -> str:
    """Format detected errors with type-specific prompts"""
    if not detected_errors:
        return ""
    
    # Separate Axe errors from static errors
    axe_errors = [e for e in detected_errors if e.startswith("ERROR AXE:")]
    static_errors = [e for e in detected_errors if not e.startswith("ERROR AXE:")]
    
    categories = _categorize_errors(static_errors)
    
    prompts = []
    
    # Add Axe errors first (they are more specific)
    if axe_errors:
        error_list = "\n".join([f"\n{i+1}. {e}" for i, e in enumerate(axe_errors)])
        prompts.append(f"""🔴 AXE ERRORS DETECTED ({len(axe_errors)} found):
These are REAL errors detected by the Axe accessibility tool on the rendered application. You MUST fix ALL of them without exception.

{error_list}

ACTION REQUIRED FOR EACH ERROR:
1. Locate the element in the template using:
   - The CSS selector provided (e.g. "button[type=\"submit\"] > .mdc-button__label")
     * Axe selectors may have specific CSS classes - look for them in the template
     * If the selector has ">" (direct child), look for the parent > child structure in the template
     * If the selector has classes like ".mdc-button__label", look for elements with class="..." that contain that class
   - Or the HTML fragment shown (it may have Angular dynamic attributes that you should ignore)
     * Ignore Angular dynamic attributes like _ngcontent-* and _nghost-*
     * Search by text content, static attributes, and structure
   - IMPORTANT: If you don't find the exact selector, look for variations:
     * Search by contained text (e.g. "Login", "Save", etc.)
     * Search by similar CSS classes
     * Search by similar HTML structure

2. Fix the specific error:
   - If it's "color-contrast":
     * CRITICAL: These are REAL errors detected on the rendered application. You MUST fix ALL of them.
     * The contrast data shows the REAL colour in the rendered HTML (after CSS is applied)
     * If the template already has style="color: ..." but Axe detects a different colour, the CSS is overriding it
     * MANDATORY FIX: Add !important to the inline style so it overrides the CSS: style="color: #000000 !important;"
     * Correction rules:
       - If current ratio < 4.5 (normal text) or < 3.0 (large text), contrast is INSUFFICIENT and MUST be fixed
       - On LIGHT backgrounds (white, light grey, etc.): use DARK text (color="#000000" or color="#212121")
       - On DARK backgrounds (black, dark grey, dark colours): use LIGHT text (color="#FFFFFF" or color="#F5F5F5")
       - Example: If Axe detects ratio 3.33 (insufficient), background is #ff4081 (pink), text is #ffffff (white),
         change text to dark colour: style="color: #000000 !important;" or change background to a lighter one
       - ALWAYS add !important to ensure the style applies over existing CSS
     * LOCATION: Find the element using the CSS selector provided (e.g. "button[type=\"submit\"] > .mdc-button__label")
       or find the HTML fragment shown in the template
       * ⚠️ CRITICAL - Elements generated by Angular Material:
         If the selector points to ".mdc-button__label", ".mat-button-label", or any element with " > " pointing to an internal span/div,
         that element does NOT exist in your template - Angular Material generates it automatically in the rendered DOM.
         
         SPECIFIC EXAMPLE:
         - Axe error: Selector ".mat-warn > .mdc-button__label", HTML "<span class="mdc-button__label">Get Started</span>"
         - In your template you will find: <button mat-button color="warn">Get Started</button>
         - FIX: Add the style to the PARENT BUTTON:
           <button mat-button color="warn" style="color: #000000 !important;">Get Started</button>
         - The style with !important will apply to the text inside the button, including the internal span generated by Angular Material
         
         GENERAL RULE:
         - If the selector has " > .mdc-button__label" or " > .mat-button-label", find the parent button in the template
         - Extract the parent selector (the part before " > ")
         - Find that button in the template (it may have color="warn", class="mat-warn", or the button text)
         - Apply style="color: [correct-color] !important;" directly to the button
         - If ratio is insufficient and background is light (#fafafa, white, etc.), use dark colour (#000000)
         - If ratio is insufficient and background is dark, use light colour (#FFFFFF)
   - If it's "link-name" or "button-name": Add descriptive aria-label to the link/button
   - If it's another error: Follow the description and help provided

3. For contrast errors:
   - The data shows the REAL colour detected by Axe in the rendered HTML
   - If the template has a different colour, the CSS is overriding it
   - You MUST use !important in the inline style to ensure it applies: style="color: #000000 !important;"
   - Do NOT return the code without fixing these errors - they are REAL errors that exist in the application

⚠️ CRITICAL: These errors EXIST in the rendered application. Do NOT return the same code. You MUST make visible changes.""")
    
    # Add categorised static errors
    for error_type, errors in categories.items():
        if errors:
            prompts.append(_build_error_specific_prompt(error_type, errors))
    
    if not prompts:
        return ""
    
    return f"""

🚨 ACCESSIBILITY ERRORS DETECTED - FIX ALL:

{chr(10).join(prompts)}

⚠️ CRITICAL: You MUST fix ALL these errors. Do NOT return the original code unchanged.
"""


def _analyze_template_for_accessibility_errors(template_content: str, style_content: Optional[str] = None) -> List[str]:
    """Analyse the template and CSS for obvious accessibility errors using raw text analysis"""
    errors = []
    import re
    
    try:
        # Raw-text analysis to handle Angular better
        lines = template_content.split('\n')
        
        # Look for buttons without text or aria-label (search in raw HTML)
        button_pattern = r'<button[^>]*>'
        for i, line in enumerate(lines, 1):
            if re.search(button_pattern, line, re.IGNORECASE):
                # Check if it has aria-label (static or binding)
                has_aria_label = (
                    'aria-label=' in line or 
                    '[attr.aria-label]' in line or
                    'aria-labelledby=' in line
                )
                # Extract button content (text between > and <)
                button_match = re.search(r'<button[^>]*>(.*?)</button>', line, re.DOTALL | re.IGNORECASE)
                if button_match:
                    button_content = button_match.group(1)
                    # Limpiar contenido Angular y HTML
                    button_text = re.sub(r'\{[^}]*\}|<[^>]+>|\*ng[A-Za-z]*="[^"]*"', '', button_content).strip()
                    # Si no tiene texto visible ni aria-label, es un error
                    if not button_text and not has_aria_label:
                        errors.append(f"Line {i}: Button without visible text or aria-label")
                elif not has_aria_label:
                    # Button may span multiple lines
                    errors.append(f"Line {i}: Button possibly without aria-label (verify manually)")
        
        # Buscar enlaces sin texto descriptivo
        link_pattern = r'<a[^>]*>'
        for i, line in enumerate(lines, 1):
            if re.search(link_pattern, line, re.IGNORECASE):
                has_aria_label = (
                    'aria-label=' in line or 
                    '[attr.aria-label]' in line
                )
                link_match = re.search(r'<a[^>]*>(.*?)</a>', line, re.DOTALL | re.IGNORECASE)
                if link_match:
                    link_text = re.sub(r'\{[^}]*\}|<[^>]+>', '', link_match.group(1)).strip()
                    if not link_text and not has_aria_label:
                        errors.append(f"Line {i}: Link without text or aria-label")
                    elif link_text.lower().strip() in ['click aquí', 'más', 'aquí', 'click here', 'more', 'here', 'more info', 'ver más', 'read more']:
                        errors.append(f"Line {i}: Link with generic text '{link_text}' needs descriptive aria-label")
        
        # Buscar inputs sin label (buscar por id y for)
        input_pattern = r'<(input|select|textarea)[^>]*>'
        input_ids = []
        label_fors = []
        
        for i, line in enumerate(lines, 1):
            # Buscar inputs y sus IDs
            input_match = re.search(input_pattern, line, re.IGNORECASE)
            if input_match:
                id_match = re.search(r'\bid=["\']([^"\']+)["\']', line)
                if id_match:
                    input_ids.append(id_match.group(1))
                else:
                    # Input sin ID - verificar si tiene aria-label
                    has_aria_label = (
                        'aria-label=' in line or 
                        '[attr.aria-label]' in line or
                        'aria-labelledby=' in line
                    )
                    if not has_aria_label:
                        errors.append(f"Line {i}: Input without id or aria-label (needs associated label)")
            
            # Buscar labels y sus atributos for
            label_match = re.search(r'<label[^>]*>', line, re.IGNORECASE)
            if label_match:
                for_match = re.search(r'\bfor=["\']([^"\']+)["\']', line)
                if for_match:
                    label_fors.append(for_match.group(1))
        
        # Verificar inputs sin label asociado
        for inp_id in input_ids:
            if inp_id not in label_fors:
                # Check if the input has aria-label on a nearby line
                found_aria = False
                for line in lines:
                    if inp_id in line and ('aria-label=' in line or '[attr.aria-label]' in line):
                        found_aria = True
                        break
                if not found_aria:
                    errors.append(f"Input con id='{inp_id}' sin label asociado (usar <label for=\"{inp_id}\">)")
        
        # Look for images without alt
        img_pattern = r'<img[^>]*>'
        for i, line in enumerate(lines, 1):
            if re.search(img_pattern, line, re.IGNORECASE):
                if 'alt=' not in line:
                    errors.append(f"Line {i}: Image without alt attribute")
        
        # Look for elements with text that may have contrast issues
        # Look for <p>, <a>, <span>, <div>, <h1-h6> without explicit colour
        text_elements_pattern = r'<(p|a|span|div|h[1-6]|label|button)[^>]*>'
        for i, line in enumerate(lines, 1):
            if re.search(text_elements_pattern, line, re.IGNORECASE):
                # Verificar si tiene texto visible
                element_match = re.search(r'<(p|a|span|div|h[1-6]|label|button)[^>]*>(.*?)</\1>', line, re.DOTALL | re.IGNORECASE)
                if element_match:
                    element_text = re.sub(r'\{[^}]*\}|<[^>]+>', '', element_match.group(2)).strip()
                    if element_text and len(element_text) > 10:  # Solo si tiene texto significativo
                        # Check if it has explicit colour
                        has_explicit_color = (
                            'style=' in line and ('color:' in line or 'color=' in line) or
                            '[style.color]' in line or
                            '[ngStyle]' in line
                        )
                        # Check if it has classes that may cause issues
                        has_problematic_class = any(cls in line for cls in ['text-muted', 'text-secondary', 'text-light', 'text-gray', 'btn'])
                        if not has_explicit_color and (has_problematic_class or 'class=' in line):
                            errors.append(f"Line {i}: Possible contrast error - {element_match.group(1)} with text without explicit colour (add style='color: #000000')")
        
        # Analizar CSS para detectar posibles problemas de contraste
        if style_content:
            contrast_errors = _analyze_css_for_contrast_issues(style_content, lines)
            errors.extend(contrast_errors)
        
    except Exception as e:
        print(f"  ⚠️ Error analizando template: {e}")
        import traceback
        traceback.print_exc()
    
    return errors


def _analyze_css_for_contrast_issues(style_content: str, template_lines: List[str]) -> List[str]:
    """Analiza el CSS para detectar posibles problemas de contraste"""
    errors = []
    import re
    
    try:
        # Buscar clases comunes que suelen tener problemas de contraste
        problematic_classes = ['text-muted', 'text-secondary', 'text-light', 'text-white', 'text-gray-300', 'text-gray-400', 'text-gray-500']
        for j, template_line in enumerate(template_lines, 1):
            for problematic_class in problematic_classes:
                if problematic_class in template_line:
                    errors.append(f"Line {j}: Possible contrast error - class '{problematic_class}' detected (add style='color: #000000')")
        
        # Buscar colores claros en el CSS
        css_lines = style_content.split('\n')
        for i, css_line in enumerate(css_lines, 1):
            # Buscar reglas de color que puedan tener bajo contraste
            if re.search(r'color\s*:', css_line, re.IGNORECASE):
                # Check if it's a light colour (simple heuristic)
                color_match = re.search(r'color\s*:\s*(#[a-f0-9]{3,6}|rgba?\([^)]+\))', css_line, re.IGNORECASE)
                if color_match:
                    color_value = color_match.group(1).lower()
                    # Detectar colores claros
                    if (color_value.startswith('#f') or color_value.startswith('#e') or 
                        color_value.startswith('#d') or 
                        ('rgba' in color_value and any(x in color_value for x in ['0.8', '0.7', '0.6', '0.5']))):
                        # Buscar el selector asociado
                        selector_match = re.search(r'^[^{]+', css_line)
                        if selector_match:
                            selector = selector_match.group(0).strip()
                            # Buscar si este selector se usa en el template
                            for j, template_line in enumerate(template_lines, 1):
                                if selector.replace('.', '').replace('#', '') in template_line:
                                    errors.append(f"Line {j}: Possible contrast error - light colour '{color_value}' detected in CSS")
                                    break
        
    except Exception as e:
        # No fallar si hay error analizando CSS
        pass
    
    return errors


def _build_component_prompt(
    component_name: str,
    template_content: str,
    ts_content: Optional[str],
    style_content: Optional[str],
    template_path: str,
    ts_path: Optional[str],
    style_path: Optional[str],
    detected_errors: List[str] = None,
    contrast_errors_count: int = 0,
) -> str:
    ts_section = f"\n---\nTypeScript ({ts_path}):\n```ts\n{ts_content}\n```" if ts_content is not None else "\n---\nTypeScript: (no proporcionado)"
    style_section = (
        f"\n---\nEstilos ({style_path}):\n```css\n{style_content}\n```"
        if style_content is not None
        else "\n---\nEstilos: (no proporcionados)"
    )

    # Build specific errors section
    errors_section = _format_detected_errors(detected_errors if detected_errors else [])
    
    # If no errors detected, use a shorter prompt
    if not detected_errors:
        return f"""Angular component: {component_name}
Template: {template_path}

TASK: Review and fix ALL accessibility errors (WCAG 2.2 A+AA) you find.

Look specifically for:
- Buttons/links without visible text or aria-label
- Inputs without associated <label>
- Images without alt attribute
- Elements with low colour contrast (minimum ratio 4.5:1)
- Interactive elements without keyboard support

IMPORTANT: If you find errors, FIX them. Do NOT return the code unchanged.

Current template:
```html
{template_content}
```
{ts_section}
{style_section}

Response format:
<<<TEMPLATE>>>
...corrected HTML template...
<<<END TEMPLATE>>>
<<<TYPESCRIPT>>>
...updated or original TypeScript...
<<<END TYPESCRIPT>>>
<<<STYLES>>>
...updated or original styles...
<<<END STYLES>>>
""".strip()
    
    # If errors were detected, use a more focused prompt
    return f"""Angular component: {component_name}
Template: {template_path}

TASK: Fix ALL the accessibility errors listed below.

{errors_section}

GENERAL RULES:
- Keep all Angular logic (bindings, *ngIf, *ngFor, pipes, etc.)
- For ARIA attributes with dynamic binding: use [attr.aria-*] instead of aria-*
- For static values: use aria-label="fixed text"
- Do NOT add HTML comments or metadata about fixes

🚨 PRESERVE RESPONSIVE AND VISUAL DESIGN (CRITICAL):
If SCREENSHOTS were provided above, they ARE your visual reference. The final design must look IDENTICAL to the screenshots.

- Do NOT change display:none to display:block - if a label is visually hidden, use aria-label on the input or an sr-only class for the label
- Do NOT add inline styles that break responsive (fixed width, excessive margin/padding, etc.)
- Keep all existing responsive classes (col-sm-*, col-md-*, etc.)
- Do NOT modify layout properties (display, position, flex, grid, width, height, margin, padding) unless critical for accessibility
- If an element has display:none for responsive design, do NOT change it - use aria-label for accessibility instead
- For contrast errors: ONLY adjust text colour (use !important if needed), do NOT change background or layout
- FIX ALL accessibility errors, but do it "invisibly" - the visual result must be identical to the screenshots

Current template:
```html
{template_content}
```
{ts_section}
{style_section}

Response format:
<<<TEMPLATE>>>
...corrected HTML template...
<<<END TEMPLATE>>>
<<<TYPESCRIPT>>>
...updated or original TypeScript...
<<<END TYPESCRIPT>>>
<<<STYLES>>>
...updated or original styles...
<<<END STYLES>>>
""".strip()


def _parse_component_response(response_text: str) -> Dict[str, Optional[str]]:
    sections = {
        "template": _extract_between_markers(response_text, "<<<TEMPLATE>>>", "<<<END TEMPLATE>>>"),
        "typescript": _extract_between_markers(response_text, "<<<TYPESCRIPT>>>", "<<<END TYPESCRIPT>>>"),
        "styles": _extract_between_markers(response_text, "<<<STYLES>>>", "<<<END STYLES>>>"),
    }

    for key, value in sections.items():
        if value is not None:
            # Strip markdown from code (```ts, ```typescript, ```css, ```scss, etc.)
            value = _clean_code_from_markdown(value)
            sections[key] = value.strip()

    if sections["template"] is None:
        raise ValueError("Model response does not contain the required <<<TEMPLATE>>> section.")

    return sections


def _clean_code_from_markdown(code: str) -> str:
    """
    Strip any markdown the LLM may have included from the code.
    Removes markdown code blocks (```ts, ```typescript, ```css, etc.)
    """
    import re
    
    # Remove markdown code blocks at the start
    # Pattern: ```ts, ```typescript, ```css, ```scss, ```html, etc.
    code = re.sub(r'^```[a-z]*\s*\n?', '', code, flags=re.MULTILINE)
    
    # Eliminar cierre de bloques markdown al final
    code = re.sub(r'\n?```\s*$', '', code, flags=re.MULTILINE)
    
    # Remove any remaining ``` in the code
    code = re.sub(r'```[a-z]*', '', code)
    code = re.sub(r'```', '', code)
    
    return code.strip()


def _extract_between_markers(text: str, start_marker: str, end_marker: str) -> Optional[str]:
    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker)
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    return text[start_idx + len(start_marker) : end_idx].strip()


def _apply_automatic_contrast_fixes(template_content: str, contrast_errors: List[str]) -> str:
    """Apply automatic contrast fixes to detected elements"""
    import re
    
    lines = template_content.split('\n')
    corrected_lines = []
    
    for i, line in enumerate(lines, 1):
        corrected_line = line
        
        # Look for contrast errors that mention this line
        for error in contrast_errors:
            if f"Line {i}:" in error:
                # Extraer el tipo de elemento del error
                element_match = re.search(r'Line \d+: Possible contrast error - (\w+)', error)
                if element_match:
                    element_type = element_match.group(1)
                    
                    # Find the element on the line
                    element_pattern = rf'<{element_type}[^>]*>'
                    element_match_in_line = re.search(element_pattern, line, re.IGNORECASE)
                    
                    if element_match_in_line:
                        element_tag = element_match_in_line.group(0)
                        
                        # Verificar si ya tiene style
                        if 'style=' not in element_tag:
                            # Add style="color: #000000"
                            corrected_tag = element_tag.rstrip('>') + ' style="color: #000000">'
                            corrected_line = line.replace(element_tag, corrected_tag)
                            print(f"    → Line {i}: Added style='color: #000000' to <{element_type}>")
                        elif 'color:' not in element_tag and 'color=' not in element_tag:
                            # Has style but no colour, add colour
                            if 'style="' in element_tag:
                                corrected_tag = element_tag.replace('style="', 'style="color: #000000; ')
                            elif "style='" in element_tag:
                                corrected_tag = element_tag.replace("style='", "style='color: #000000; ")
                            else:
                                # style sin comillas (raro pero posible)
                                corrected_tag = element_tag.rstrip('>') + ' style="color: #000000">'
                            corrected_line = line.replace(element_tag, corrected_tag)
                            print(f"    → Line {i}: Added colour: #000000 to existing style of <{element_type}>")
        
        corrected_lines.append(corrected_line)
    
    return '\n'.join(corrected_lines)


def _fix_responsive_breaking_changes(original: str, corrected: str) -> str:
    """
    Detect and fix changes that break responsive design.
    Specifically reverts display:none to display:block changes on labels.
    """
    if not original or not corrected:
        return corrected
    
    import re
    
    # Buscar en el original labels con display:none (en style o como atributo)
    original_display_none_labels = re.findall(
        r'<label[^>]*(?:style="[^"]*display\s*:\s*none[^"]*"|class="[^"]*visually-hidden[^"]*")[^>]*>.*?</label>',
        original,
        re.DOTALL | re.IGNORECASE
    )
    
    # Also look for labels with hidden attribute
    original_hidden_labels = re.findall(
        r'<label[^>]*hidden[^>]*>.*?</label>',
        original,
        re.DOTALL | re.IGNORECASE
    )
    
    all_original_labels = original_display_none_labels + original_hidden_labels
    
    if not all_original_labels:
        return corrected
    
    # For each hidden label in the original, check if it was changed in the corrected one
    for original_label in all_original_labels:
        # Extraer el contenido del label (texto entre > y <)
        label_match = re.search(r'<label[^>]*>(.*?)</label>', original_label, re.DOTALL)
        if not label_match:
            continue
        
        label_content = label_match.group(1).strip()
        # Buscar el for attribute
        for_attr_match = re.search(r'for="([^"]+)"', original_label)
        if not for_attr_match:
            continue
        
        for_value = for_attr_match.group(1)
        
        # Check in the corrected version if that label was changed to display:block
        pattern_block = rf'<label[^>]*for="{re.escape(for_value)}"[^>]*style="[^"]*display\s*:\s*block[^"]*"[^>]*>'
        # Also check if hidden or display:none was removed
        pattern_no_hidden = rf'<label[^>]*for="{re.escape(for_value)}"[^>]*(?!style="[^"]*display\s*:\s*none)(?!class="[^"]*visually-hidden)(?!hidden)[^>]*>'
        
        needs_fix = False
        if re.search(pattern_block, corrected, re.IGNORECASE):
            needs_fix = True
        elif re.search(pattern_no_hidden, corrected, re.IGNORECASE):
            # Verificar que no tenga display:none ni visually-hidden en el corregido
            corrected_label_match = re.search(
                rf'<label[^>]*for="{re.escape(for_value)}"[^>]*>.*?</label>',
                corrected,
                re.DOTALL | re.IGNORECASE
            )
            if corrected_label_match:
                corrected_label_full = corrected_label_match.group(0)
                if 'display:none' not in corrected_label_full.lower() and 'visually-hidden' not in corrected_label_full.lower() and 'hidden' not in corrected_label_full.lower():
                    needs_fix = True
        
        if needs_fix:
            # LLM changed display:none/hidden to visible - revert it
            corrected_label_match = re.search(
                rf'<label[^>]*for="{re.escape(for_value)}"[^>]*>.*?</label>',
                corrected,
                re.DOTALL | re.IGNORECASE
            )
            if corrected_label_match:
                corrected_label_full = corrected_label_match.group(0)
                # Extraer el atributo for y el contenido
                label_id_match = re.search(r'for="([^"]+)"', corrected_label_full)
                label_content_match = re.search(r'<label[^>]*>(.*?)</label>', corrected_label_full, re.DOTALL)
                
                if label_id_match and label_content_match:
                    new_label = f'<label for="{label_id_match.group(1)}" class="visually-hidden">{label_content_match.group(1).strip()}</label>'
                    corrected = corrected.replace(corrected_label_full, new_label)
                    print(f"  ⚠️ Detectado cambio que rompe responsive: label con display:block revertido a visually-hidden")
    
    return corrected


def _apply_automatic_accessibility_fixes(template_content: Optional[str]) -> Optional[str]:
    """
    Apply common automatic accessibility fixes that the LLM may not do consistently.

    Fixes applied:
    1. Add role="img" to <i> and <nb-icon> elements that have aria-label but no role
    2. Add lang attribute to <html> if missing
    3. Add aria-label to role="progressbar" elements that don't have it
    """
    if not template_content:
        return template_content
    
    import re
    corrected = template_content
    
    # 1. Add role="img" to <i> with aria-label but no role
    # Pattern: <i ... aria-label="..." ...> (no role)
    pattern_i_with_aria = r'(<i\s+[^>]*aria-label="[^"]*"[^>]*?)(?<!role="[^"]*")(?<!role=\'[^\']*\')([^>]*>)'
    def add_role_to_i(match):
        full_tag = match.group(0)
        # Si ya tiene role, no hacer nada
        if 'role=' in full_tag:
            return full_tag
        # Add role="img" before closing >
        return full_tag[:-1] + ' role="img">'
    
    # Buscar <i> con aria-label sin role
    i_tags = re.finditer(r'<i\s+[^>]*aria-label="[^"]*"[^>]*>', corrected)
    for match in list(i_tags):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    # 2. Add role="img" to <nb-icon> with aria-label but no role
    # Buscar <nb-icon ... aria-label="..." ...> (sin role)
    nb_icon_tags = re.finditer(r'<nb-icon\s+[^>]*aria-label="[^"]*"[^>]*>', corrected)
    for match in list(nb_icon_tags):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    # Also handle [attr.aria-label] (dynamic binding)
    nb_icon_tags_dynamic = re.finditer(r'<nb-icon\s+[^>]*\[attr\.aria-label\]="[^"]*"[^>]*>', corrected)
    for match in list(nb_icon_tags_dynamic):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    # 3. Add lang attribute to <html> if missing
    if '<html' in corrected and 'lang=' not in corrected.split('<html')[1].split('>')[0]:
        corrected = re.sub(r'(<html)([^>]*>)', r'\1 lang="en"\2', corrected, count=1)
    
    # 4. Add aria-label to elements with role="progressbar" that don't have it
    progressbar_pattern = r'(<[^>]*\s+role="progressbar"[^>]*?)(?<!aria-label="[^"]*")(?<!aria-labelledby="[^"]*")([^>]*>)'
    def add_aria_to_progressbar(match):
        full_tag = match.group(0)
        # Si ya tiene aria-label o aria-labelledby, no hacer nada
        if 'aria-label=' in full_tag or 'aria-labelledby=' in full_tag:
            return full_tag
        # Extraer el valor de aria-valuenow si existe para crear un label descriptivo
        valuenow_match = re.search(r'aria-valuenow="([^"]*)"', full_tag)
        valuenow = valuenow_match.group(1) if valuenow_match else ""
        label_text = f"Progress: {valuenow}%" if valuenow else "Progress indicator"
        # Add aria-label before closing >
        return full_tag[:-1] + f' aria-label="{label_text}">'
    
    progressbar_tags = re.finditer(r'<[^>]*\s+role="progressbar"[^>]*>', corrected)
    for match in list(progressbar_tags):
        tag = match.group(0)
        if 'aria-label=' not in tag and 'aria-labelledby=' not in tag:
            # Crear un label descriptivo
            valuenow_match = re.search(r'aria-valuenow="([^"]*)"', tag)
            valuenow = valuenow_match.group(1) if valuenow_match else ""
            label_text = f"Progress: {valuenow}%" if valuenow else "Progress indicator"
            corrected = corrected.replace(tag, tag[:-1] + f' aria-label="{label_text}">', 1)
    
    return corrected


def _fix_basic_syntax_errors(template_content: Optional[str]) -> Optional[str]:
    """
    Fix common basic HTML syntax errors that the LLM may introduce.
    Specifically fixes attributes without closing quotes: attr="value> -> attr="value">
    """
    if not template_content:
        return template_content
    
    import re
    
    corrected = template_content
    
    # Strategy: process line by line and fix unclosed attributes
    lines = corrected.split('\n')
    fixed_lines = []
    
    for line in lines:
        fixed_line = line
        
        # 1. Corregir atributos que terminan con > sin comilla de cierre
        # Ejemplos:
        #   aria-label="texto>  -> aria-label="texto">
        #   style="color: #000000 !important;>  -> style="color: #000000 !important;">
        #   for="email>  -> for="email">
        
        # Find all attributes on the line: attr="value>
        # Pattern: word-attr="anything-without-quotes>
        # But exclude template references (#ref) that don't use quotes

        # Approach: look for specific unclosed-attribute patterns
        # Case 1: attr="text> where text doesn't contain quotes
        # Use a pattern that captures attribute, =", value, and >
        # then add the quote before >

        def fix_unclosed_attr_in_line(text):
            """Fix attributes missing closing quote on a line"""
            result = text

            # Look for patterns: attr="value> where > is immediately after the value
            # This includes both normal attributes and Angular bindings

            # Pattern 1: Normal attributes: attr="value>
            # Also captures Angular bindings: [attr]="expression>, (event)="handler()>, etc.
            # Pattern must capture: name-attr="value-content>
            # Where value-content can have spaces, special chars, Angular expressions, etc.

            # Improved pattern that also captures Angular bindings
            # Looks for: (event)="...>, [attr]="...>, *directive="...>, etc.
            pattern = r'([(\[\*#]?[\w-]+(?:\([^)]*\))?[\]\)]?)="([^"]*?)([^">])\s*>'
            
            def replace_attr(match):
                attr_name = match.group(1)
                attr_value = match.group(2)
                last_char = match.group(3)
                
                # Ensure it's not a template reference (#ref)
                if attr_name.startswith('#'):
                    return match.group(0)
                
                # If value is not empty, add quote before >
                return f'{attr_name}="{attr_value}{last_char}">'
            
            result = re.sub(pattern, replace_attr, result)
            
            # Most common specific cases
            # Corregir: style="...!important;> -> style="...!important;">
            result = re.sub(r'(style="[^"]*?)\s*!important\s*;>', r'\1 !important;">', result)
            # Corregir: style="color: #000000> -> style="color: #000000;">
            result = re.sub(r'(style="[^"]*?[^";])\s*>', r'\1;">', result)
            
            # Corregir atributos data-*: data-bs-target="#modal>texto -> data-bs-target="#modal">texto
            # This pattern captures attributes that end just before a word (not before >)
            result = re.sub(r'(data-[\w-]+="[^"]*?)>([A-Za-z])', r'\1">\2', result)
            
            # Corregir otros atributos: attr="valor> -> attr="valor">
            # Pero evitar duplicar comillas si ya hay una
            result = re.sub(r'([\w-]+)="([^"]*?[^"])\s*>(?!")', r'\1="\2">', result)
            
            return result
        
        fixed_line = fix_unclosed_attr_in_line(fixed_line)
        
        # 2. Corregir template references (#ref) que tienen comillas incorrectas
        # Template references NO deben tener comillas: #stepper"> -> #stepper>
        fixed_line = re.sub(r'#(\w+)">', r'#\1>', fixed_line)
        fixed_line = re.sub(r'#(\w+)\s*">', r'#\1>', fixed_line)
        
        # 3. Known specific cases
        fixed_line = fixed_line.replace('#stepper">', '#stepper>')
        fixed_line = fixed_line.replace('#picker">', '#picker>')
        fixed_line = fixed_line.replace('#drawer">', '#drawer>')
        
        fixed_lines.append(fixed_line)
    
    corrected = '\n'.join(fixed_lines)
    
    return corrected


def _fix_angular_aria_syntax(template_content: Optional[str]) -> Optional[str]:
    """Corrige la sintaxis de atributos ARIA en templates Angular para usar [attr.aria-*] con binding"""
    if not template_content:
        return template_content
    
    import re
    
    # Pattern to find aria-* with interpolation binding {{ }}
    # Ejemplo: aria-pressed="{{condicion}}" -> [attr.aria-pressed]="condicion"
    pattern_interpolation = r'aria-([a-z-]+)="{{([^}]+)}}"'
    def replace_interpolation(match):
        attr_name = match.group(1)
        expression = match.group(2).strip()
        return f'[attr.aria-{attr_name}]="{expression}"'
    
    corrected = re.sub(pattern_interpolation, replace_interpolation, template_content)
    
    # Pattern to find aria-* with interpolation in strings
    # Ejemplo: aria-label="Texto {{variable}}" -> [attr.aria-label]="'Texto ' + variable"
    pattern_string_interpolation = r'aria-([a-z-]+)="([^"]*)\{\{([^}]+)\}\}([^"]*)"'
    def replace_string_interpolation(match):
        attr_name = match.group(1)
        before = match.group(2)
        expression = match.group(3).strip()
        after = match.group(4)
        # Build concatenated expression
        parts = []
        if before:
            parts.append(f"'{before}'")
        parts.append(expression)
        if after:
            parts.append(f"'{after}'")
        return f'[attr.aria-{attr_name}]="{" + ".join(parts)}"'
    
    corrected = re.sub(pattern_string_interpolation, replace_string_interpolation, corrected)
    
    return corrected


def _apply_changes_map(changes_map: List[Dict], project_root: Path) -> int:
    """Apply the change map to the actual source code"""
    applied_count = 0
    for change_entry in changes_map:
        changes = change_entry.get("changes", {})
        for file_type, file_change in changes.items():
            try:
                target_path = Path(file_change["path"])
                target_path.write_text(file_change["corrected"], encoding="utf-8")
                applied_count += 1
            except Exception as e:
                print(f"  ⚠️ Error aplicando cambio en {file_change['path']}: {e}")
    return applied_count


def _revert_changes(changes_map: List[Dict], project_root: Path) -> None:
    """Revierte los cambios aplicados restaurando el contenido original"""
    for change_entry in changes_map:
        changes = change_entry.get("changes", {})
        for file_type, file_change in changes.items():
            try:
                target_path = Path(file_change["path"])
                target_path.write_text(file_change["original"], encoding="utf-8")
            except Exception as e:
                print(f"  ⚠️ Error revirtiendo cambio en {file_change['path']}: {e}")


def _verify_angular_build(project_root: Path) -> Tuple[bool, bool]:
    """
    Verifica que el proyecto Angular compile correctamente ejecutando ng build.
    
    Returns:
        Tuple of (compilation success, verification available).
        If verification is not available (ng not found), returns (True, False).
        para no bloquear el proceso.
    """
    # Detectar si es un workspace multi-proyecto
    default_project = _get_default_project_name(project_root)
    project_arg = [default_project] if default_project else []
    if default_project:
        print(f"  → Workspace multi-proyecto detectado, compilando: {default_project}")
    
    # Strategy 1: Try npm run build (most common in Angular projects)
    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            with open(package_json, 'r', encoding='utf-8') as f:
                package_data = json.load(f)
                scripts = package_data.get("scripts", {})
                if "build" in scripts:
                    print("  → Using 'npm run build' to verify compilation...")
                    result = subprocess.run(
                        ["npm", "run", "build"],
                        cwd=str(project_root),
                        capture_output=True,
                        text=True,
                        encoding='utf-8',
                        errors='replace',
                        timeout=300
                    )
                    if result.returncode == 0:
                        return True, True
                    else:
                        # Show compilation errors if any
                        if result.stderr:
                            print(f"  Compilation errors:\n{result.stderr[:500]}")
                        return False, True
        except Exception as e:
            pass
    
    # Estrategia 2: Intentar con ng directamente
    try:
        build_cmd = ["ng", "build"] + project_arg + ["--configuration", "production"]
        result = subprocess.run(
            build_cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=300
        )
        if result.returncode == 0:
            return True, True
        else:
            if result.stderr:
                print(f"  Compilation errors:\n{result.stderr[:500]}")
            return False, True
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        print("  ⚠️ Timeout al compilar el proyecto")
        return False, True
    except Exception:
        pass
    
    # Estrategia 3: Intentar con npx
    try:
        build_cmd = ["npx", "-y", "@angular/cli", "build"] + project_arg + ["--configuration", "production"]
        result = subprocess.run(
            build_cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=300
        )
        if result.returncode == 0:
            return True, True
        else:
            if result.stderr:
                print(f"  Compilation errors:\n{result.stderr[:500]}")
            return False, True
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        print("  ⚠️ Timeout al compilar el proyecto")
        return False, True
    except Exception:
        pass
    
    # Estrategia 4: Intentar con node_modules/.bin/ng
    node_modules_ng = project_root / "node_modules" / ".bin" / "ng"
    if node_modules_ng.exists():
        try:
            # En Windows, puede ser ng.cmd
            ng_cmd = str(node_modules_ng)
            if not ng_cmd.endswith('.cmd') and (project_root / "node_modules" / ".bin" / "ng.cmd").exists():
                ng_cmd = str(project_root / "node_modules" / ".bin" / "ng.cmd")
            
            build_cmd = [ng_cmd, "build"] + project_arg + ["--configuration", "production"]
            result = subprocess.run(
                build_cmd,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=300
            )
            if result.returncode == 0:
                return True, True
            else:
                if result.stderr:
                    print(f"  Compilation errors:\n{result.stderr[:500]}")
                return False, True
        except Exception as e:
            print(f"  ⚠️ Error running ng from node_modules: {e}")

    # If no strategy works, assume verification is not possible
    print("  ⚠️ Could not run ng build (ng not found in PATH, npx not available, or node_modules not found)")
    print("  → Continuing without compilation verification")
    return True, False  # Return (True, False) to indicate verification was not possible but do not block


def _compile_and_get_errors(project_root: Path) -> Dict:
    """
    Compile the Angular project and return compilation errors if any.

    Returns:
        Dict with:
        - success: bool - Whether compilation succeeded
        - verification_available: bool - Whether verification was possible
        - errors: List[str] - List of compilation errors
        - output: str - Full compilation output
    """
    errors = []
    output = ""
    success = True
    verification_available = False
    
    # Siempre ejecutar build para capturar errores, independientemente de _verify_angular_build
    try:
        # Ejecutar build y capturar stderr y stdout
        package_json = project_root / "package.json"
        if package_json.exists():
            try:
                with open(package_json, 'r', encoding='utf-8') as f:
                    package_data = json.load(f)
                    scripts = package_data.get("scripts", {})
                    if "build" in scripts:
                        result = subprocess.run(
                            ["npm", "run", "build"],
                            cwd=str(project_root),
                            capture_output=True,
                            text=True,
                            timeout=300
                        )
                        output = result.stderr + result.stdout
                        verification_available = True
                        # Parsear errores incluso si returncode == 0 (puede haber errores de TypeScript)
                        errors = _parse_angular_errors(output)
                        if errors:
                            success = False
                            print(f"  → Build completed but {len(errors)} errors found, parsing...")
                        elif result.returncode != 0:
                            success = False
                            print(f"  → Build failed, parsing errors...")
            except Exception as e:
                print(f"  ⚠️ Error ejecutando npm run build: {e}")
        
        # Si no se obtuvieron errores o no hay script build, intentar con ng build
        if not verification_available or (not errors and not success):
            try:
                # Detectar si es un workspace multi-proyecto
                default_project = _get_default_project_name(project_root)
                build_cmd = ["ng", "build"]
                if default_project:
                    build_cmd.append(default_project)
                    print(f"  → Workspace multi-proyecto detectado, compilando proyecto: {default_project}")
                
                result = subprocess.run(
                    build_cmd,
                    cwd=str(project_root),
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=300
                )
                output = result.stderr + result.stdout
                verification_available = True
                # Parsear errores incluso si returncode == 0 (puede haber errores de TypeScript)
                if not errors:  # Solo parsear si no se obtuvieron errores antes
                    errors = _parse_angular_errors(output)
                    if errors:
                        success = False
                        print(f"  → Build completed but {len(errors)} errors found, parsing...")
                    elif result.returncode != 0:
                        success = False
                        print(f"  → Build failed, parsing errors...")
            except Exception as e:
                print(f"  ⚠️ Error ejecutando ng build: {e}")
    except Exception as e:
        print(f"  ⚠️ Error general en _compile_and_get_errors: {e}")
    
    # Si no se pudo verificar, usar _verify_angular_build como fallback
    if not verification_available:
        result = _verify_angular_build(project_root)
        success, verification_available = result
    
    return {
        "success": success,
        "verification_available": verification_available,
        "errors": errors,
        "output": output
    }


def _parse_angular_errors(build_output: str) -> List[str]:
    """Parse Angular compilation errors from the output"""
    errors = []
    lines = build_output.split('\n')
    
    current_error = []
    in_error_block = False
    
    # First, look for specific TypeScript/Angular errors that can appear even when the build "completes"
    for i, line in enumerate(lines):
        # Look for lines that indicate errors (more specific)
        # Incluir errores que empiezan con ./src/ (webpack errors)
        # Also look for "Module not found" or "Can't resolve" directly
        # Buscar patrones de error TS y NG incluso sin el prefijo "ERROR"
        is_error_line = (
            'ERROR' in line.upper() or 
            'error TS' in line.lower() or 
            'error NG' in line.lower() or 
            (line.strip().startswith('./src/') and ('Error:' in line or 'Error' in line or 'Module not found' in line or "Can't resolve" in line)) or
            'Module not found' in line or 
            "Can't resolve" in line or 
            'Cannot find module' in line or
            # Patrones adicionales para errores de TypeScript
            (line.strip().startswith('src/') and 'error TS' in line.lower()) or
            (line.strip().startswith('Error:') and ('TS' in line or 'NG' in line))
        )
        
        if is_error_line:
            if current_error:
                errors.append('\n'.join(current_error))
                current_error = []
            current_error.append(line)
            in_error_block = True
        elif in_error_block:
            # Keep adding error lines until we find a blank line or a new error
            if line.strip() == '' and current_error:
                # Blank line may indicate end of error, but continue if there is context
                if len(current_error) > 1:
                    current_error.append(line)
                else:
                    if current_error:
                        errors.append('\n'.join(current_error))
                        current_error = []
                        in_error_block = False
            elif (line.strip().startswith('src/') or line.strip().startswith('./src/') or 
                  ':' in line or line.strip().startswith('Error occurs') or 
                  'error TS' in line.lower() or 'error NG' in line.lower() or
                  'Cannot find module' in line or "Can't resolve" in line or
                  'imports:' in line or 'import {' in line):
                current_error.append(line)
            elif current_error and (line.strip() or 'at ' in line or '^' in line):
                # Error context lines (stack trace, location, etc.)
                current_error.append(line)
            else:
                # Fin del bloque de error
                if current_error:
                    errors.append('\n'.join(current_error))
                    current_error = []
                    in_error_block = False
    
    if current_error:
        errors.append('\n'.join(current_error))
    
    # Filter empty errors
    errors = [e for e in errors if e.strip()]
    
    return errors[:20]  # Limitar a 20 errores


def _fix_compilation_errors(errors: List[str], project_root: Path, client) -> List[Dict]:
    """
    Fix compilation errors using LLM and automatic fixes.
    
    Returns:
        Lista de correcciones a aplicar
    """
    if not errors:
        return []
    
    fixes = []
    
    # First, apply automatic fixes for common missing-module errors
    import re
    print(f"  → Analysing {len(errors)} errors for automatic fixes...")
    for i, error in enumerate(errors):
        # Buscar errores de "Module not found" o "Cannot find module"
        if 'Module not found' in error or 'Cannot find module' in error or "Can't resolve" in error:
            print(f"    Error {i+1}: Missing module error detected")
            print(f"      First lines: {error.split(chr(10))[0][:150]}...")
            
            # Extract module name and file path
            module_match = re.search(r"Can't resolve '([^']+)'|Cannot find module '([^']+)'|Module not found.*?'([^']+)'", error)
            file_match = re.search(r'(?:\./)?src/([^\s:]+\.(?:ts|html|scss|css|sass))', error)
            
            if module_match:
                module_name = module_match.group(1) or module_match.group(2) or module_match.group(3)
                print(f"      Module detected: {module_name}")
            else:
                print(f"      ⚠️ Could not extract module name")
                module_name = None
            
            if file_match:
                file_path = 'src/' + file_match.group(1)
                print(f"      Archivo detectado: {file_path}")
            else:
                print(f"      ⚠️ No se pudo extraer la ruta del archivo")
                file_path = None
            
            if module_match and file_match and module_name:
                full_path = project_root / file_path
                
                if full_path.exists():
                    print(f"  → Applying automatic fix for missing module: {module_name} in {file_path}")
                    try:
                        content = full_path.read_text(encoding='utf-8')
                        corrected_content = _auto_fix_missing_module(content, module_name)
                        
                        if corrected_content != content:
                            # Aplicar inmediatamente
                            full_path.write_text(corrected_content, encoding='utf-8')
                            fixes.append({
                                "path": file_path,
                                "original": content,
                                "corrected": corrected_content
                            })
                            print(f"    ✓ Automatic fix applied and saved to {file_path}")
                        else:
                            print(f"    ⚠️ No se detectaron cambios en {file_path}")
                    except Exception as e:
                        print(f"    ⚠️ Error in automatic fix: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    print(f"    ⚠️ Archivo no existe: {full_path}")
            else:
                print(f"    ⚠️ Could not extract module or file from error")
    
    # First, try to install missing modules automatically
    missing_modules = []
    for error in errors:
        # Buscar errores de "Module not found" o "Cannot find module"
        if 'Module not found' in error or 'Cannot find module' in error or "Can't resolve" in error:
            # Extract the module name
            module_match = re.search(r"Can't resolve '([^']+)'|Cannot find module '([^']+)'", error)
            if module_match:
                module_name = module_match.group(1) or module_match.group(2)
                if module_name and module_name not in missing_modules:
                    missing_modules.append(module_name)
    
    # Try to install missing modules
    if missing_modules:
        print(f"  → {len(missing_modules)} missing modules detected, attempting to install...")
        for module in missing_modules:
            try:
                print(f"    → Instalando {module}...")
                result = subprocess.run(
                    ["npm", "install", module],
                    cwd=str(project_root),
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=120
                )
                if result.returncode == 0:
                    print(f"    ✓ {module} instalado correctamente")
                else:
                    print(f"    ⚠️ No se pudo instalar {module}: {result.stderr[:200]}")
            except Exception as e:
                print(f"    ⚠️ Error instalando {module}: {e}")
    
    # Agrupar errores por archivo
    errors_by_file = {}
    for error in errors:
        # Extraer ruta del archivo del error
        file_path = None
        for line in error.split('\n'):
            # Buscar patrones de ruta de archivo en el error
            if 'src/' in line or './src/' in line or 'projects/' in line:
                import re
                # Patrones posibles:
                # - src/path/to/file.ts
                # - ./src/path/to/file.ts
                # - projects/xxx/src/path/to/file.ts
                match = re.search(
                    r'((?:\./)?(?:projects/[^\s:]+/)?src/[^\s:]+\.(ts|html|scss|css|sass))',
                    line
                )
                if match:
                    potential_path = match.group(1)
                    if potential_path.startswith('./'):
                        potential_path = potential_path[2:]
                    full_path = project_root / potential_path
                    if full_path.exists():
                        file_path = potential_path
                        break
        
        if file_path:
            if file_path not in errors_by_file:
                errors_by_file[file_path] = []
            errors_by_file[file_path].append(error)
        else:
            # If no file found, add to "unknown" for debugging
            if "unknown" not in errors_by_file:
                errors_by_file["unknown"] = []
            errors_by_file["unknown"].append(error)
    
    # Corregir errores archivo por archivo
    print(f"  → Encontrados errores en {len([f for f in errors_by_file.keys() if f != 'unknown'])} archivo(s)")
    if "unknown" in errors_by_file:
        print(f"  ⚠️ {len(errors_by_file['unknown'])} error(s) could not be associated with a specific file")
    
    for file_path, file_errors in list(errors_by_file.items())[:10]:  # Limitar a 10 archivos
        if file_path == "unknown":
            print(f"  ⚠️ Saltando {len(file_errors)} error(es) sin archivo asociado")
            continue
            
        try:
            full_path = project_root / file_path
            if not full_path.exists():
                continue
                
            original_content = full_path.read_text(encoding='utf-8')
            errors_text = '\n\n'.join(file_errors[:3])  # Limitar a 3 errores por archivo
            
            # Usar LLM para corregir errores
            system_message = "You are an expert in Angular and TypeScript. Fix the compilation errors without changing functionality."
            
            # Detect if there are missing module errors
            has_missing_module = 'Module not found' in errors_text or 'Cannot find module' in errors_text or "Can't resolve" in errors_text
            
            if has_missing_module:
                # Extract the missing module name from the error
                import re
                module_name = None
                module_match = re.search(r"Can't resolve '([^']+)'|Cannot find module '([^']+)'|Module not found.*'([^']+)'", errors_text)
                if module_match:
                    module_name = module_match.group(1) or module_match.group(2) or module_match.group(3)
                
                prompt = f"""
Fix the following Angular compilation errors in the file {file_path}:

Errors:
{errors_text}

IMPORTANT: The module '{module_name if module_name else "unknown"}' cannot be found or does not exist in npm.
You MUST do the following:
1. COMMENT OUT or REMOVE the import of the missing module
2. COMMENT OUT or REMOVE all uses of the module in the code (in @Component imports, in code, etc.)
3. If the module is used in the @Component imports array, REMOVE it from that array
4. Add an explanatory comment: // Module not available: {module_name if module_name else "missing module"}

Example:
- If you have: import {{CKEditorModule}} from "@angular/ckeditor5-angular";
- Change to: // import {{CKEditorModule}} from "@angular/ckeditor5-angular"; // Module not available
- And remove CKEditorModule from the @Component imports array

Current file content:
```typescript
{original_content[:3000]}
```

Fix ONLY the compilation errors. COMMENT OUT or REMOVE the import and ALL its uses.
Return the full corrected code without the missing module.
"""
            else:
                prompt = f"""
Fix the following Angular compilation errors in the file {file_path}:

Errors:
{errors_text}

Current file content:
```typescript
{original_content[:3000]}
```

Fix ONLY the compilation errors. Keep all existing functionality and logic.
Return the full corrected code.
"""
            
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
            )
            
            corrected_content = response.choices[0].message.content.strip()
            log_openai_call(prompt=prompt, response=corrected_content, model="gpt-4o", call_type="angular_compilation_fix")
            
            # Clean the corrected code (remove markdown if present)
            if corrected_content.startswith('```'):
                parts = corrected_content.split('```')
                if len(parts) >= 3:
                    # Extract content between code blocks
                    code_block = parts[1]
                    if code_block.startswith('typescript') or code_block.startswith('ts') or code_block.startswith('html'):
                        code_block = code_block.split('\n', 1)[1] if '\n' in code_block else ''
                    corrected_content = code_block.strip()
                else:
                    # Si no hay cierre, intentar extraer de otra forma
                    corrected_content = corrected_content.replace('```typescript', '').replace('```ts', '').replace('```html', '').replace('```', '').strip()
            
            corrected_content = corrected_content.strip()
            
            if corrected_content and corrected_content != original_content.strip():
                print(f"    ✓ Fix generated for {file_path}")
                fixes.append({
                    "path": str(full_path),
                    "original": original_content,
                    "corrected": corrected_content,
                    "errors": file_errors
                })
            else:
                print(f"    ⚠️ No valid fix generated for {file_path}")
        except Exception as e:
            print(f"  ⚠️ Error corrigiendo {file_path}: {e}")
            import traceback
            traceback.print_exc()
    
    return fixes


def _auto_fix_missing_module(content: str, module_name: str) -> str:
    """Automatically fix a missing module by commenting out the import and removing its uses"""
    import re
    
    lines = content.split('\n')
    corrected_lines = []
    module_short_names = []
    
    # Extract short module name (e.g. CKEditorModule from @angular/ckeditor5-angular)
    import_pattern = rf'import\s+\{{([^}}]+)\}}\s+from\s+["\']{re.escape(module_name)}["\']'
    import_match = re.search(import_pattern, content)
    if import_match:
        imports_str = import_match.group(1)
        # There may be multiple imports separated by commas
        module_short_names = [name.strip() for name in imports_str.split(',')]
        print(f"      → Modules detected in import: {module_short_names}")
    else:
        print(f"      ⚠️ Import for {module_name} not found")
    
    import_commented = False
    imports_removed = False
    
    for i, line in enumerate(lines):
        original_line = line
        
        # Comment out the missing module import
        if module_name in line and 'import' in line and 'from' in line:
            # Comment out the full line
            if not line.strip().startswith('//'):
                # Preserve indentation
                indent = len(line) - len(line.lstrip())
                corrected_lines.append(' ' * indent + f"// {line.strip()} // Module not available: {module_name}")
                import_commented = True
                print(f"      → Import comentado: {line.strip()[:60]}...")
            else:
                corrected_lines.append(line)
        # Remove the module from the @Component imports array
        elif module_short_names and any(name in line for name in module_short_names):
            # Check if this line contains the imports array
            if 'imports:' in line or ('imports' in line and '[' in line):
                # Remove each module from the array
                original_line_for_log = line
                for module_short_name in module_short_names:
                    if module_short_name in line:
                        # Remove the module from the array with different patterns
                        # Pattern 1: , ModuleName,
                        line = re.sub(rf',\s*{re.escape(module_short_name)}\s*,', ',', line)
                        # Pattern 2: , ModuleName]
                        line = re.sub(rf',\s*{re.escape(module_short_name)}\s*\]', ']', line)
                        # Pattern 3: [ModuleName,
                        line = re.sub(rf'\[\s*{re.escape(module_short_name)}\s*,', '[', line)
                        # Pattern 4: [ModuleName]
                        line = re.sub(rf'\[\s*{re.escape(module_short_name)}\s*\]', '[]', line)
                        # Limpiar comas dobles
                        line = re.sub(r',\s*,', ',', line)
                        # Limpiar espacios extra alrededor de comas
                        line = re.sub(r',\s+', ', ', line)
                if line != original_line_for_log:
                    imports_removed = True
                    print(f"      → Module removed from imports array: {original_line_for_log.strip()[:60]}...")
                corrected_lines.append(line)
            else:
                corrected_lines.append(line)
        else:
            corrected_lines.append(line)
    
    if not import_commented:
        print(f"      ⚠️ No import was commented out")
    if not imports_removed:
        print(f"      ⚠️ No module was removed from the imports array")
    
    return '\n'.join(corrected_lines)


def _apply_compilation_fixes(fixes: List[Dict], project_root: Path) -> None:
    """Apply the compilation fixes"""
    for fix in fixes:
        try:
            target_path = Path(fix["path"])
            target_path.write_text(fix["corrected"], encoding="utf-8")
        except Exception as e:
            print(f"  ⚠️ Error applying fix to {fix['path']}: {e}")


def _start_angular_dev_server(project_root: Path, port: int = 4200, wait_for_ready: bool = False):
    """Inicia el servidor de desarrollo Angular (ng serve) en el puerto especificado
    
    Args:
        project_root: Ruta al proyecto Angular
        port: Puerto donde iniciar el servidor (default: 4200)
        wait_for_ready: Si True, inicia el servidor en background y retorna el proceso. Si False, ejecuta en foreground.
    
    Returns:
        subprocess.Popen si wait_for_ready=True, None si wait_for_ready=False
    """
    import socket
    
    # Check if the port is available
    def is_port_available(port_num: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('localhost', port_num))
                return True
            except OSError:
                return False
    
    # Verificar puerto 4200
    if not is_port_available(port):
        print(f"  ⚠️ Port {port} is in use.")
        response = input(f"  ¿Deseas usar otro puerto? (s/n): ")
        if response.lower() == 's':
            # Buscar puerto disponible
            for p in range(4201, 4210):
                if is_port_available(p):
                    port = p
                    print(f"  → Usando puerto {port}")
                    break
            else:
                print("  ⚠️ No available port found. Using default port.")
                port = 4200
        else:
            print("  → Intentando usar el puerto 4200 de todas formas...")
    
    # Helper to check if a command exists
    def command_exists(cmd):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=2,
                check=False
            )
            # If the command exists, it will return 0 or 1 (not FileNotFoundError)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        except Exception:
            return False
    
    # Estrategia 1: Intentar con npm run start primero
    package_json = project_root / "package.json"
    if package_json.exists():
        try:
            with open(package_json, 'r', encoding='utf-8') as f:
                package_data = json.load(f)
                scripts = package_data.get("scripts", {})
                if "start" in scripts:
                    # Verificar que npm existe
                    if command_exists(["npm", "--version"]):
                        print(f"  → Iniciando servidor con 'npm start' en puerto {port}...")
                        print("  Presiona Ctrl+C para detener el servidor.")
                        try:
                            # Modify the start script to use the specific port if needed
                            subprocess.run(
                                ["npm", "start", "--", "--port", str(port)],
                                cwd=str(project_root),
                                check=False
                            )
                            return
                        except KeyboardInterrupt:
                            print("\n  Servidor detenido por el usuario.")
                            return
                        except Exception as e:
                            print(f"  ⚠️ Error con 'npm start': {e}")
        except Exception:
            pass
    
    # Estrategia 2: Intentar con ng serve directamente
    if command_exists(["ng", "version"]):
        try:
            print(f"  → Iniciando servidor con 'ng serve --port {port}'...")
            if wait_for_ready:
                process = subprocess.Popen(
                    ["ng", "serve", "--port", str(port)],
                    cwd=str(project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
                return process
            else:
                print("  Presiona Ctrl+C para detener el servidor.")
                subprocess.run(
                    ["ng", "serve", "--port", str(port), "--open"],
                    cwd=str(project_root),
                    check=False
                )
                return None
        except KeyboardInterrupt:
            if process:
                process.terminate()
            print("\n  Servidor detenido por el usuario.")
            return None
        except Exception as e:
            print(f"  ⚠️ Error con 'ng serve': {e}")
    
    # Estrategia 3: Intentar con npx
    if command_exists(["npx", "--version"]):
        try:
            print(f"  → Iniciando servidor con 'npx ng serve --port {port}'...")
            if wait_for_ready:
                process = subprocess.Popen(
                    ["npx", "-y", "@angular/cli", "serve", "--port", str(port)],
                    cwd=str(project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
                return process
            else:
                print("  Presiona Ctrl+C para detener el servidor.")
                subprocess.run(
                    ["npx", "-y", "@angular/cli", "serve", "--port", str(port), "--open"],
                    cwd=str(project_root),
                    check=False
                )
                return None
        except KeyboardInterrupt:
            if process:
                process.terminate()
            print("\n  Servidor detenido por el usuario.")
            return None
        except Exception as e:
            print(f"  ⚠️ Error con 'npx ng serve': {e}")
    
    # Estrategia 4: Intentar con node_modules/.bin/ng (Windows y Unix)
    ng_cmd_path = None
    # En Windows, buscar .cmd primero
    if (project_root / "node_modules" / ".bin" / "ng.cmd").exists():
        ng_cmd_path = str(project_root / "node_modules" / ".bin" / "ng.cmd")
    elif (project_root / "node_modules" / ".bin" / "ng.bat").exists():
        ng_cmd_path = str(project_root / "node_modules" / ".bin" / "ng.bat")
    elif (project_root / "node_modules" / ".bin" / "ng").exists():
        # En Windows, puede necesitar ejecutarse con cmd /c
        import sys
        if sys.platform == "win32":
            ng_cmd_path = str(project_root / "node_modules" / ".bin" / "ng.cmd")
            if not Path(ng_cmd_path).exists():
                # Si no existe .cmd, intentar ejecutar el script directamente con node
                ng_script = project_root / "node_modules" / ".bin" / "ng"
                if ng_script.exists():
                    # Read the shebang to see how to run it
                    try:
                        with open(ng_script, 'r', encoding='utf-8') as f:
                            first_line = f.readline()
                            if first_line.startswith('#!'):
                                # Es un script, necesitamos ejecutarlo con node
                                ng_cmd_path = None  # Will be handled differently
                    except Exception:
                        pass
    
    if ng_cmd_path and Path(ng_cmd_path).exists():
        try:
            print(f"  → Iniciando servidor con '{ng_cmd_path} serve --port {port} --open'...")
            print("  Presiona Ctrl+C para detener el servidor.")
            subprocess.run(
                [ng_cmd_path, "serve", "--port", str(port), "--open"],
                cwd=str(project_root),
                check=False
            )
            return
        except KeyboardInterrupt:
            print("\n  Servidor detenido por el usuario.")
            return
        except Exception as e:
            print(f"  ⚠️ Error ejecutando ng desde node_modules: {e}")
    
    print("  ⚠️ Could not start the server (ng not found in any location)")
    print(f"  → Puedes iniciarlo manualmente con: ng serve --port {port}")


def _write_if_changed(target_path: Path, new_content: Optional[str], original_content: str) -> bool:
    if new_content is None:
        return False
    if new_content.strip() == original_content.strip():
        return False
    target_path.write_text(new_content, encoding="utf-8")
    return True
