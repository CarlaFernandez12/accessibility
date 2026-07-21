"""
React accessibility workflows and Axe-driven component corrections.

This module encapsulates logic specific to React projects:
    - Mapping violations back to JSX/TSX components.
    - Guiding LLM-based fixes for affected components.
    - Providing a small compatibility entrypoint for fix-only flows.

Support helpers such as project detection, component discovery, Axe execution,
and dev-server startup live in core.react_support.
"""

import base64
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.contrast_engine import (
    apply_source_catalog_contrast_repair,
    build_node_contrast_issue,
    extract_contrast_payload,
    load_color_catalog,
    split_contrast_violations,
)
from core.react_support import (
    detect_react_project,
    discover_react_components,
    jsx_contains_html_elements,
    normalize_react_html,
    run_axe_on_react_app,
    start_react_dev_server,
)
from utils.io_utils import log_openai_call


def _resolve_react_source_roots(project_root: Path, source_roots: Optional[List[Path]]) -> List[Path]:
    """Resolve candidate React source roots for Axe-to-component mapping."""
    if source_roots is not None:
        return source_roots

    possible_roots = [
        project_root / "src",
        project_root / "app",
        project_root / "components",
        project_root / "pages",
        project_root,
    ]
    resolved_roots = [root for root in possible_roots if root.exists()]
    return resolved_roots or [project_root / "src"]


def _discover_react_components_for_mapping(project_root: Path, source_roots: List[Path]) -> List[Path]:
    """Discover React components for violation mapping, including the existing project-wide fallback."""
    print(f"[React + Axe] Searching for components in: {[str(root) for root in source_roots]}")

    all_found_components: List[Path] = []
    for root in source_roots:
        found = discover_react_components([root])
        all_found_components.extend(found)
        print(f"[React + Axe] Found {len(found)} component(s) in {root}")

    if all_found_components:
        return all_found_components

    print("[React + Axe] Warning: no components found in the expected directories; searching the whole project...")
    if not project_root.exists():
        print(f"[React + Axe] Warning: project directory does not exist: {project_root}")
        return []

    all_found_components = discover_react_components([project_root])
    print(f"[React + Axe] Found {len(all_found_components)} component(s) in the whole project.")

    if all_found_components:
        return all_found_components

    print("[React + Axe] Warning: no component candidates matched; listing a few discovered files for diagnostics.")
    try:
        js_files = [f for f in project_root.glob("**/*.js") if "node_modules" not in str(f)][:10]
        jsx_files = [f for f in project_root.glob("**/*.jsx") if "node_modules" not in str(f)][:10]
        ts_files = [f for f in project_root.glob("**/*.ts") if "node_modules" not in str(f)][:10]
        tsx_files = [f for f in project_root.glob("**/*.tsx") if "node_modules" not in str(f)][:10]
        print(f"[React + Axe] .js files found: {len(js_files)} sample(s)")
        if js_files:
            print(f"[React + Axe] Samples: {[str(f.relative_to(project_root)) for f in js_files[:3]]}")
        print(f"[React + Axe] .jsx files found: {len(jsx_files)} sample(s)")
        if jsx_files:
            print(f"[React + Axe] Samples: {[str(f.relative_to(project_root)) for f in jsx_files[:3]]}")
        print(f"[React + Axe] .ts files found: {len(ts_files)} sample(s)")
        print(f"[React + Axe] .tsx files found: {len(tsx_files)} sample(s)")
    except Exception as exc:
        print(f"[React + Axe] Warning: failed to list diagnostic files: {exc}")

    return all_found_components


def _match_component_by_selector(selector: str, components: Dict[str, Dict[str, str]]) -> Tuple[Optional[str], str]:
    """Match a React component using the Axe target selector when possible."""
    if not selector:
        return None, ""

    # Remove Angular runtime attributes that never exist in source templates.
    selector = re.sub(r'\[_ngcontent-[^\]]+\]', '', selector)
    selector = re.sub(r'\[_nghost-[^\]]+\]', '', selector)
    selector = re.sub(r'\[_ngcontent-[^=\]]+="[^"]*"\]', '', selector)
    selector = re.sub(r'\[_nghost-[^=\]]+="[^"]*"\]', '', selector)
    selector = re.sub(r'\s+', ' ', selector).strip()

    if not selector:
        return None, ""

    if selector.startswith('.'):
        class_name = selector[1:]
        for rel_path, comp_data in components.items():
            if class_name in comp_data["jsx"]:
                return rel_path, f"target selector (class: {class_name})"
        return None, ""

    if '[' in selector and ']' in selector:
        attr = selector.split('[')[1].split(']')[0]
        for rel_path, comp_data in components.items():
            if attr in comp_data["jsx"]:
                return rel_path, f"target selector (attr: {attr})"
        return None, ""

    for rel_path, comp_data in components.items():
        if selector in comp_data["jsx"]:
            return rel_path, f"target selector (raw: {selector})"

    return None, ""


def _match_component_by_normalized_content(
    normalized_snippet: str,
    components: Dict[str, Dict[str, str]],
) -> Tuple[Optional[str], str]:
    """Match a React component by normalized rendered HTML content."""
    if not normalized_snippet:
        return None, ""

    for rel_path, comp_data in components.items():
        if normalized_snippet in comp_data["normalized"]:
            return rel_path, "normalized content"

    return None, ""


def _match_component_by_css_classes(
    html_snippet: str,
    components: Dict[str, Dict[str, str]],
) -> Tuple[Optional[str], str]:
    """Match a React component by CSS classes present in the rendered HTML snippet."""
    if not html_snippet:
        return None, ""

    classes_in_snippet = re.findall(r'class=["\']([^"\']+)["\']', html_snippet)
    if not classes_in_snippet:
        return None, ""

    all_classes = " ".join(classes_in_snippet).split()
    snippet_tag = re.search(r'<([a-zA-Z][a-zA-Z0-9_-]*)', html_snippet)
    if not snippet_tag:
        return None, ""

    tag_name = snippet_tag.group(1)
    best_match: Optional[str] = None
    best_score = 0
    for rel_path, comp_data in components.items():
        matching_classes = [
            css_class
            for css_class in all_classes
            if re.search(rf'(?<![\w-]){re.escape(css_class)}(?![\w-])', comp_data["jsx"])
        ]
        if len(matching_classes) < min(2, len(all_classes)):
            continue
        if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
            score = len(matching_classes)
            if any(css_class.startswith("bi-") for css_class in matching_classes):
                score += 2
            if any(css_class.startswith("btn-outline-") for css_class in matching_classes):
                score += 1
            if score > best_score:
                best_match = rel_path
                best_score = score

    if best_match:
        matching_classes = [
            css_class
            for css_class in all_classes
            if re.search(rf'(?<![\w-]){re.escape(css_class)}(?![\w-])', components[best_match]["jsx"])
        ]
        return best_match, f"CSS classes ({', '.join(matching_classes[:3])})"

    return None, ""


def _match_component_by_visible_text(
    html_snippet: str,
    components: Dict[str, Dict[str, str]],
) -> Tuple[Optional[str], str]:
    """Match a React component by visible text extracted from the rendered HTML snippet."""
    if not html_snippet:
        return None, ""

    text_content = re.sub(r'<[^>]+>', '', html_snippet).strip()
    text_content = re.sub(r'\s+', ' ', text_content)
    if len(text_content) <= 3:
        return None, ""

    snippet_tag = re.search(r'<([a-zA-Z][a-zA-Z0-9_-]*)', html_snippet)
    if not snippet_tag:
        return None, ""

    tag_name = snippet_tag.group(1)
    for rel_path, comp_data in components.items():
        if text_content not in comp_data["jsx"]:
            continue
        if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
            return rel_path, f"exact visible text: '{text_content[:30]}...'"

    words = [word for word in text_content.split() if len(word) > 3]
    if not words:
        return None, ""

    for rel_path, comp_data in components.items():
        matching_words = [word for word in words if word in comp_data["jsx"]]
        if len(matching_words) < min(2, len(words)):
            continue
        if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
            return rel_path, f"visible text (words: {', '.join(matching_words[:3])})"

    return None, ""


def _match_component_by_raw_tags(
    html_snippet: str,
    normalized_snippet: str,
    components: Dict[str, Dict[str, str]],
) -> Tuple[Optional[str], str]:
    """Match a React component by raw JSX/tag presence as a fallback strategy."""
    snippet_tag = re.search(r'<([a-zA-Z][a-zA-Z0-9_-]*)', html_snippet)
    if not snippet_tag:
        return None, ""

    tag_name = snippet_tag.group(1)
    for rel_path, comp_data in components.items():
        if not jsx_contains_html_elements(comp_data["jsx"], normalized_snippet):
            continue
        if f'<{tag_name}' in comp_data["jsx"] or f'<{tag_name} ' in comp_data["jsx"]:
            return rel_path, "tag match"

    return None, ""


def _match_iframe_component_fallback(
    html_snippet: str,
    components: Dict[str, Dict[str, str]],
) -> Tuple[Optional[str], str]:
    """Apply the existing iframe-specific fallback mapping strategy."""
    if "iframe" not in html_snippet.lower():
        return None, ""

    common_names = ["App.js", "App.jsx", "App.tsx", "index.js", "index.jsx"]
    for rel_path in components.keys():
        if any(name in rel_path for name in common_names):
            return rel_path, "common component (iframe)"

    for rel_path, comp_data in components.items():
        if "position" in comp_data["jsx"] and "fixed" in comp_data["jsx"]:
            return rel_path, "indicador CSS (iframe)"

    if components:
        return next(iter(components.keys())), "fallback (iframe)"

    return None, ""


def map_axe_violations_to_react_components(
    axe_results: Dict, project_root: Path, source_roots: Optional[List[Path]] = None
) -> Dict[str, List[Dict]]:
    """
    Map Axe violations from rendered HTML back to React components (*.jsx, *.tsx).

    Uses the same general strategy as Angular, adapted for JSX.
    """
    if not axe_results:
        return {}
    
    violations = axe_results.get("violations", []) or []
    if not violations:
        return {}
    
    wcag_violations = [
        v for v in violations 
        if v.get("impact") in ["critical", "serious"]
    ]
    
    if not wcag_violations:
        print("[React + Axe] No WCAG A/AA violations were found (critical/serious).")
        print(f"[React + Axe] Total violations detected: {len(violations)}")
        if violations:
            impacts = {}
            for v in violations:
                impact = v.get("impact", "unknown")
                impacts[impact] = impacts.get(impact, 0) + 1
            print(f"[React + Axe] Distribution by impact: {impacts}")
        return {}
    
    print("[React + Axe] Filtering WCAG A/AA violations:")
    print(f"  - Total violations detected: {len(violations)}")
    print(f"  - WCAG A/AA violations (critical/serious): {len(wcag_violations)}")
    
    source_roots = _resolve_react_source_roots(project_root, source_roots)

    components: Dict[str, Dict[str, str]] = {}
    all_found_components = _discover_react_components_for_mapping(project_root, source_roots)
    
    print(f"[React + Axe] Total components found: {len(all_found_components)}")
    
    if all_found_components:
        print("[React + Axe] Sample components found:")
        for comp in all_found_components[:5]:
            print(f"  - {comp.relative_to(project_root) if project_root in comp.parents else comp}")
    
    for comp_path in all_found_components:
        try:
            rel_path = comp_path.relative_to(project_root)
            jsx_content = comp_path.read_text(encoding="utf-8")
            searchable_content = jsx_content

            # Angular-style projects can be misdetected as React. If this TS component points to
            # an external HTML template, include it for matching selectors/snippets.
            if comp_path.suffix == ".ts":
                template_match = re.search(r'templateUrl\s*:\s*["\']([^"\']+)["\']', jsx_content)
                if template_match:
                    template_rel = template_match.group(1)
                    template_path = (comp_path.parent / template_rel).resolve()
                else:
                    template_path = comp_path.with_suffix(".html")

                if template_path.exists():
                    try:
                        template_html = template_path.read_text(encoding="utf-8")
                        searchable_content = f"{jsx_content}\n\n{template_html}"
                    except Exception:
                        pass

            normalized = normalize_react_html(searchable_content)
            components[str(rel_path)] = {
                "jsx": searchable_content,
                "normalized": normalized,
            }
        except Exception as e:
            print(f"[React + Axe] Warning: failed to load {comp_path}: {e}")
            continue
    
    issues_by_component: Dict[str, List[Dict]] = {}
    
    print(f"[React + Axe] Mapping {len(wcag_violations)} WCAG A/AA violation(s) to components...")
    
    for violation in wcag_violations:
        violation_id = violation.get("id", "")
        violation_description = violation.get("description", "")
        impact = violation.get("impact", "unknown")
        wcag_level = "WCAG A" if impact == "critical" else "WCAG AA" if impact == "serious" else "Other"
        print(f"  → Violation [{wcag_level}]: {violation_id} - {violation_description} (impact: {impact})")

        for node in violation.get("nodes", []):
            html_snippet = node.get("html") or ""
            if not html_snippet:
                continue

            normalized_snippet = normalize_react_html(html_snippet)
            if not normalized_snippet.strip():
                continue

            targets = node.get("target", [])
            selector = targets[0] if targets and isinstance(targets[0], str) else ""

            matched_component = None
            match_method = ""

            matched_component, match_method = _match_component_by_selector(selector, components)

            # 2) Search on normalised content
            if not matched_component:
                matched_component, match_method = _match_component_by_normalized_content(
                    normalized_snippet,
                    components,
                )

            if not matched_component and html_snippet:
                matched_component, match_method = _match_component_by_css_classes(
                    html_snippet,
                    components,
                )

            if not matched_component:
                matched_component, match_method = _match_component_by_raw_tags(
                    html_snippet,
                    normalized_snippet,
                    components,
                )

            if not matched_component and html_snippet:
                matched_component, match_method = _match_component_by_visible_text(
                    html_snippet,
                    components,
                )
            
            if not matched_component:
                matched_component, match_method = _match_iframe_component_fallback(
                    html_snippet,
                    components,
                )
            
            if matched_component:
                if matched_component not in issues_by_component:
                    issues_by_component[matched_component] = []

                normalized_issue = (
                    build_node_contrast_issue(violation, node)
                    if violation.get("id") == "color-contrast"
                    else {
                        "violation": violation,
                        "node": node,
                    }
                )
                issues_by_component[matched_component].append(normalized_issue)
                if "fallback" in match_method:
                    print(f"    ⚠️ Mapped with fallback to {matched_component} (method: {match_method})")
                    print(f"      Note: No exact match found, using default component")
                else:
                    print(f"    ✓ Mapped to {matched_component} (method: {match_method})")
            else:
                html_preview = html_snippet[:100].replace('\n', ' ') if html_snippet else "N/A"
                print(f"    Warning: could not map selector {selector[:50] if selector else 'N/A'}...")
                print(f"      HTML snippet: {html_preview}...")
                if selector:
                    class_name = selector.lstrip('.').split()[0] if selector.startswith('.') else ""
                    if class_name:
                        print(f"      Tried to find class: {class_name}")
                print(f"      Total available components: {len(components)}")
    
    original_count = len(issues_by_component)
    filtered_issues_by_component: Dict[str, List[Dict]] = {
        rel_path: issues
        for rel_path, issues in issues_by_component.items()
        if "node_modules" not in rel_path.replace("/", "\\")
    }

    if original_count > 0 and not filtered_issues_by_component:
        print("[React + Axe] All mapped violations belong to files in node_modules.")
        print("  → No fixes will be applied to third-party code (libraries).")
        print("  → If you want to fix those issues, copy the markup into your own components under src/.")
        return {}

    issues_by_component = filtered_issues_by_component

    print(f"[React + Axe] Total components with mapped violations: {len(issues_by_component)}")
    for rel_path, issues in issues_by_component.items():
        print(f"  - {rel_path}: {len(issues)} violation(s)")
    
    print(f"[React + Axe] Axe violations were mapped to {len(issues_by_component)} component(s).")
    
    return issues_by_component


def _build_axe_based_prompt_for_react_component(
    component_path: str, component_content: str, issues: List[Dict]
) -> str:
    """
    Build a compact prompt to fix accessibility issues in a React component.
    """
    violation_lines: List[str] = []

    for issue in issues:
        if not isinstance(issue, dict):
            continue

        violation = issue.get("violation", {}) or {}
        node = issue.get("node", {}) or {}

        v_id = violation.get("id", "unknown")
        impact = violation.get("impact", "moderate")
        desc = violation.get("description", "")
        html_snippet = (node.get("html") or "").strip()

        # Tag principal del snippet
        tag = "elemento"
        m = re.search(r"<(\w+)", html_snippet)
        if m:
            tag = m.group(1)

        line = f"- {v_id} ({impact}) en <{tag}>"
        if desc:
            line += f": {desc}"

        violation_lines.append(line)

        if html_snippet:
            first_line = html_snippet.splitlines()[0].strip()
            violation_lines.append(f"  HTML: {first_line[:200]}...")

    violations_text = "\n".join(violation_lines)
    total = len(issues)

    # Detect contrast errors to give more specific instructions
    has_contrast = any(issue.get("violation", {}).get("id", "") == "color-contrast" for issue in issues)
    
    contrast_instructions = ""
    if has_contrast:
        contrast_instructions = """
🚨 CRITICAL - CONTRAST FIX:
These are REAL errors detected by Axe on the rendered application. You MUST fix ALL of them.

To fix contrast errors:
1. LOCATE the element using the HTML fragment provided in "HTML: ..."
   - Find the EXACT element in the JSX code that matches that HTML
   - Search by:
     * Contained text (e.g. "Code", "Chat on whatsapp", "Save Contact")
     * Specific CSS classes (e.g. "btn-outline-light", "btn-success", "btn-outline-dark mx-1")
     * Element structure (tag + classes + text)
   - Ignore dynamic React attributes (data-react-*, generated className, etc.)
   - If you do NOT find the element in this component, search other components in the project:
     * Search files that contain the text or classes from the HTML snippet
     * Elements may be in App.js, Home.js, Header.js, Footer.js, or other components
   - ⚠️ IMPORTANT: If the element is NOT in this component, you MUST state so clearly or search other files

2. FIX the text colour according to the background:
   - If background is LIGHT (white, light grey, light colours): use DARK text
     * style={{ color: '#000000' }} or color="#000000" or color="black"
   - If background is DARK (black, dark grey, dark colours): use LIGHT text
     * style={{ color: '#FFFFFF' }} or color="#FFFFFF" or color="white"

3. VALID FORMATS in React/JSX:
   - style={{ color: '#000000' }} (inline style)
   - color="#000000" (Chakra UI prop like <Text color="#000000">)
   - color="black" (Chakra UI prop with colour name)
   - If the element already has style={{ ... }}, add color inside the same object

4. IMPORTANT:
   - If the element uses Chakra UI (Text, Heading, Button, etc.), you can modify the color="..." prop
   - If the element is native HTML (<span>, <p>, <button>, etc.), use style={{ color: '...' }}
   - Do NOT change background colours, only text colour
   - Do NOT return the code unchanged if there are listed contrast violations

⚠️ Do NOT return the same code. You MUST make real changes to the colours."""
    
    prompt = f"""Fix ALL {total} WCAG A/AA violations in this React component.

COMPONENT: {component_path}

VIOLATIONS:
{violations_text}
{contrast_instructions}

QUICK RULES:
- color-contrast → adjust ONLY text colour (style={{ color: '...' }} or color="...") according to background
- aria-input-field-name / label → <label htmlFor="id"> or aria-label="text" on inputs/selects
- button-name → visible text or aria-label="action" on <button>
- link-name → descriptive text or aria-label="destination" on <a>
- image-alt / role-img-alt → alt="..." or aria-label="..." on images/visual roles
- frame-title → title="..." on <iframe>
- select-name → <label htmlFor> or aria-label on <select>
- target-size → padding / minWidth / minHeight for touch area (~44x44px)
- nested-interactive → avoid <button> inside <a> (and vice versa)

INSTRUCTIONS:
- Fix ONLY the elements listed in the violations list.
- PRECISE LOCATION: For each violation, find the EXACT element using:
  * Visible text from the HTML snippet (e.g. "Code", "Chat on whatsapp", "Save Contact")
  * CSS classes from the snippet (e.g. "btn-outline-light", "btn-success", "btn-outline-dark mx-1 d-flex")
  * Tag and element structure
- If you do NOT find the element in this component:
  * The element may be in another component (App.js, Home.js, Header.js, Footer.js, etc.)
  * Search the project for the text or classes from the HTML snippet
  * If you cannot access other components, state clearly that the element is not in this file
- Keep hooks, props, state and React logic unchanged.
- Do not change layout (width, height, margin, padding, display, position, flex, grid).
- Do not remove or add large JSX components; add/modify attributes on existing elements.
- ⚠️ CRITICAL: If contrast violations are listed, you MUST change the colours. Do NOT return the code unchanged.
- ⚠️ CRITICAL: If the element is NOT in this component, do NOT invent it. Search other files or state that it was not found.

FULL COMPONENT (CURRENT):
```jsx
{component_content}
```

Return ONLY the full corrected component, no explanations."""

    return prompt.strip()


def _get_specific_instruction_for_violation(violation_id: str, html_snippet: str, contrast_info: str) -> str:
    """Return a specific, concise instruction for each violation type."""
    v_lower = violation_id.lower()
    
    if "color-contrast" in v_lower:
        if contrast_info:
            # Extract contrast data in a simple way
            bg = "#ffffff"  # default
            if "Background color:" in contrast_info:
                try:
                    bg = contrast_info.split("Background color:")[1].split("\n")[0].strip()
                except Exception:
                    pass
            elif "Color de fondo:" in contrast_info:
                try:
                    bg = contrast_info.split("Color de fondo:")[1].split("\n")[0].strip()
                except:
                    pass
            recommended = "#000000" if any(c in bg.lower() for c in ["#ff", "#fff", "#00d1", "white", "light"]) else "#FFFFFF"
            return f"Add style={{'color': '{recommended}'}} to the element (background: {bg})"
        return "Add style={{'color': '#000000'}} or style={{'color': '#FFFFFF'}} according to background"
    
    if "aria-input-field-name" in v_lower or "label" in v_lower or "form-field" in v_lower:
        return "Add <label htmlFor=\"id\"> or aria-label=\"descriptive text\" to input/select/textarea"
    
    if "button-name" in v_lower:
        return "Add visible text inside the <button> or aria-label=\"action\" if it only has icons"
    
    if "link-name" in v_lower:
        return "Add descriptive text inside the <a> or aria-label=\"destination\" if it only has icons"
    
    if "image-alt" in v_lower or "img" in v_lower:
        return "Add alt=\"description\" or alt=\"\" if the image is decorative"
    
    if "frame-title" in v_lower:
        return "Add title=\"content description\" to the <iframe>"
    
    if "select-name" in v_lower:
        return "Add <label htmlFor=\"id\"> or aria-label=\"text\" to the <select>"
    
    if "target-size" in v_lower:
        return "Increase touch area (min 44x44px) with padding or minWidth/minHeight in style"
    
    if "nested-interactive" in v_lower:
        return "Separate interactive elements: no <button> inside <a>, no <a> inside <button>"
    
    if "aria-allowed-attr" in v_lower:
        return "Remove ARIA attributes not allowed for the element's role"
    
    if "aria-required-children" in v_lower:
        return "Add the required child elements for the role or change the role to a valid one"
    
    if "aria-valid-attr-value" in v_lower:
        return "Fix invalid ARIA attribute values (e.g. role=\"invalid\" → role=\"button\")"
    
    if "aria-toggle" in v_lower:
        return "Add aria-label=\"toggle state\" to the element with role=\"switch\" or role=\"checkbox\""
    
    return "Read the description and apply the minimum necessary fix"


def _build_react_fix_user_message(
    prompt: str,
    screenshot_paths: Optional[List[str]],
    has_contrast_errors: bool,
):
    """Build the user message payload for React accessibility fixes, including screenshots when useful."""
    if not screenshot_paths or not has_contrast_errors:
        return {"role": "user", "content": prompt}

    screenshot_instructions = """
📸 SCREENSHOTS - CRITICAL FOR PRESERVING DESIGN:

I have taken screenshots of the application at different screen sizes (mobile, tablet, desktop) that show how the page REALLY looks before the fixes.

🚨 MANDATORY INSTRUCTIONS ABOUT THE SCREENSHOTS:
1. EXAMINE each screenshot in detail to understand:
   - The current visual design (layout, colours, spacing, distribution)
   - How content adapts at different screen sizes
   - Which elements are visible/hidden at each size
   - The application's overall visual style
   - The REAL background colours visible in the screenshots

2. FIX ALL contrast errors listed above, BUT:
   - KEEP the visual design you see in the screenshots
   - Do NOT change background colours, element sizes, or distribution shown in the images
   - For contrast errors: adjust ONLY the text colour based on the REAL background you see in the screenshots
   - If the background is LIGHT in the screenshots: use DARK text (#000000, #212121)
   - If the background is DARK in the screenshots: use LIGHT text (#FFFFFF, #F5F5F5)
   - Do NOT add new visible elements (use aria-label or sr-only instead)
   - Do NOT change display:none to display:block if that element is not visible in the screenshots
   - Respect the responsive design: if it looks a certain way on mobile, keep it that way

3. YOUR GOAL: Fix ALL contrast errors WITHOUT changing how the page looks in the screenshots.
   - Fixes should be visually "invisible"
   - Use minimal contrast adjustments based on the REAL backgrounds you see in the screenshots
   - The final design must look IDENTICAL to the screenshots, but accessible

The screenshots show the application BEFORE the fixes. Your job is to make it accessible while keeping that exact visual appearance.

🚨 CRITICAL - DO NOT BREAK RESPONSIVE:
- Do NOT change layout properties in style: width, height, margin, padding, display, position, flex, grid
- Do NOT modify className that affect responsive behaviour
- For contrast: ONLY change text colour, do NOT touch layout or backgrounds
- The design must look IDENTICAL on mobile, tablet and desktop after the fixes
"""

    user_content = [{"type": "text", "text": prompt + screenshot_instructions}]
    for screenshot_path in screenshot_paths:
        try:
            screenshot_file = Path(screenshot_path)
            if not screenshot_file.exists():
                continue

            with open(screenshot_file, "rb") as img_file:
                image_base64 = base64.b64encode(img_file.read()).decode("utf-8")
            mime_type = "image/png"
            if screenshot_path.endswith(".jpg") or screenshot_path.endswith(".jpeg"):
                mime_type = "image/jpeg"
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                }
            )
        except Exception as exc:
            print(f"  ⚠️ Error al incluir captura {screenshot_path}: {exc}")

    return {"role": "user", "content": user_content}


def _normalize_react_llm_response(corrected: str) -> str:
    """Normalize raw LLM output into JSX code before post-processing."""
    corrected = corrected.strip()
    if not corrected.startswith("```"):
        return corrected

    parts = corrected.split("```")
    if len(parts) >= 3:
        code_block = parts[1]
        if "\n" in code_block:
            code_block = code_block.split("\n", 1)[1]
        return code_block.strip()

    return (
        corrected.replace("```jsx", "")
        .replace("```tsx", "")
        .replace("```js", "")
        .replace("```", "")
        .strip()
    )


def _validate_react_llm_response(
    rel_path: str,
    original_content: str,
    corrected: str,
    allow_html_fragment: bool = False,
) -> bool:
    """Validate that the LLM returned plausible React/JSX code without risky new elements."""
    if corrected.strip().startswith("//") or corrected.strip().startswith("/*"):
        print(f"[React + Axe] ⚠️ LLM returned a comment instead of code for {rel_path}")
        return False

    if allow_html_fragment:
        if not re.search(r'<[a-zA-Z][a-zA-Z0-9_-]*', corrected):
            print(f"[React + Axe] ⚠️ LLM did not return valid HTML-like code for {rel_path}")
            return False
    elif not re.search(r'<\w+|import\s+|export\s+|function\s+|const\s+|class\s+', corrected):
        print(f"[React + Axe] ⚠️ LLM did not return valid React/JSX code for {rel_path}")
        return False

    if len(corrected.strip()) < len(original_content.strip()) * 0.5:
        print(
            f"[React + Axe] ⚠️ La respuesta del LLM es demasiado corta para {rel_path} "
            f"({len(corrected)} vs {len(original_content)} chars)"
        )
        return False

    orig_tags = set(re.findall(r'<([a-zA-Z][a-zA-Z0-9_-]*)', original_content))
    corr_tags = set(re.findall(r'<([a-zA-Z][a-zA-Z0-9_-]*)', corrected)) if corrected else set()
    new_tags = corr_tags - orig_tags

    # Ignore TypeScript generic identifiers (typically UpperCamelCase, e.g. HTMLInputElement)
    # and focus the safety check on lowercase DOM/HTML-like tags.
    new_dom_like_tags = {tag for tag in new_tags if tag and tag[:1].islower()}
    allowed_new_tags = {"label"}
    problematic_new_tags = new_dom_like_tags - allowed_new_tags
    if problematic_new_tags:
        print(f"[React + Axe] ⚠️ LLM added disallowed new elements: {problematic_new_tags}")
        print(f"[React + Axe] ⚠️ Changes will NOT be applied to avoid introducing errors")
        return False

    return True


def _detect_react_accessibility_changes(original_content: str, corrected: str) -> Dict[str, object]:
    """Detect meaningful accessibility-related changes between original and corrected React code."""
    orig_colors_style = re.findall(r'style\s*=\s*\{\s*[^}]*color\s*:\s*["\']?([^"\';}]+)', original_content, re.IGNORECASE)
    corr_colors_style = re.findall(r'style\s*=\s*\{\s*[^}]*color\s*:\s*["\']?([^"\';}]+)', corrected, re.IGNORECASE) if corrected else []

    orig_colors_prop = re.findall(r'color\s*=\s*["\']([^"\']+)["\']', original_content, re.IGNORECASE)
    corr_colors_prop = re.findall(r'color\s*=\s*["\']([^"\']+)["\']', corrected, re.IGNORECASE) if corrected else []

    orig_colors_css = re.findall(r'color\s*:\s*["\']?([^"\';]+)', original_content, re.IGNORECASE)
    corr_colors_css = re.findall(r'color\s*:\s*["\']?([^"\';]+)', corrected, re.IGNORECASE) if corrected else []

    orig_colors = set(orig_colors_style + orig_colors_prop + orig_colors_css)
    corr_colors = set(corr_colors_style + corr_colors_prop + corr_colors_css)
    has_color_diff = orig_colors != corr_colors

    orig_normalized = re.sub(r'\s+', ' ', original_content.strip())
    corr_normalized = re.sub(r'\s+', ' ', corrected.strip()) if corrected else ""

    orig_aria = set(re.findall(r'aria-\w+=["\'][^"\']*["\']', original_content, re.IGNORECASE))
    corr_aria = set(re.findall(r'aria-\w+=["\'][^"\']*["\']', corrected, re.IGNORECASE)) if corrected else set()
    has_aria_diff = orig_aria != corr_aria

    orig_alt = set(re.findall(r'alt=["\'][^"\']*["\']', original_content, re.IGNORECASE))
    corr_alt = set(re.findall(r'alt=["\'][^"\']*["\']', corrected, re.IGNORECASE)) if corrected else set()
    has_alt_diff = orig_alt != corr_alt

    orig_labels = set(re.findall(r'<label[^>]*>', original_content, re.IGNORECASE))
    corr_labels = set(re.findall(r'<label[^>]*>', corrected, re.IGNORECASE)) if corrected else set()
    has_label_diff = orig_labels != corr_labels

    orig_styles = set(re.findall(r'style\s*=\s*\{\s*\{[^}]+\}\s*\}', original_content, re.IGNORECASE))
    corr_styles = set(re.findall(r'style\s*=\s*\{\s*\{[^}]+\}\s*\}', corrected, re.IGNORECASE)) if corrected else set()
    has_style_diff = orig_styles != corr_styles

    has_changes = (
        orig_normalized != corr_normalized
        or has_color_diff
        or has_aria_diff
        or has_alt_diff
        or has_label_diff
        or has_style_diff
    )

    return {
        "orig_colors": orig_colors,
        "corr_colors": corr_colors,
        "has_color_diff": has_color_diff,
        "orig_aria": orig_aria,
        "corr_aria": corr_aria,
        "has_aria_diff": has_aria_diff,
        "orig_alt": orig_alt,
        "corr_alt": corr_alt,
        "has_alt_diff": has_alt_diff,
        "has_changes": has_changes,
    }


def _apply_react_manual_fallbacks(
    rel_path: str,
    original_content: str,
    issues: List[Dict],
) -> Tuple[str, bool]:
    """Apply conservative manual fallbacks when the LLM response is invalid or unchanged."""

    def _find_opening_tag_ranges(markup: str, tag_names: Tuple[str, ...]) -> List[Tuple[int, int]]:
        ranges: List[Tuple[int, int]] = []
        index = 0
        while index < len(markup):
            if markup[index] != "<":
                index += 1
                continue

            matched_tag = None
            for tag_name in tag_names:
                if markup.startswith(f"<{tag_name}", index):
                    matched_tag = tag_name
                    break

            if not matched_tag:
                index += 1
                continue

            in_single_quote = False
            in_double_quote = False
            brace_depth = 0
            cursor = index + len(matched_tag) + 1
            while cursor < len(markup):
                char = markup[cursor]
                prev_char = markup[cursor - 1] if cursor > index else ""

                if char == "'" and not in_double_quote and prev_char != "\\":
                    in_single_quote = not in_single_quote
                elif char == '"' and not in_single_quote and prev_char != "\\":
                    in_double_quote = not in_double_quote
                elif not in_single_quote and not in_double_quote:
                    if char == "{":
                        brace_depth += 1
                    elif char == "}" and brace_depth > 0:
                        brace_depth -= 1
                    elif char == ">" and brace_depth == 0:
                        ranges.append((index, cursor + 1))
                        index = cursor + 1
                        break

                cursor += 1
            else:
                break

        return ranges
    has_contrast = any(issue.get("violation", {}).get("id", "") == "color-contrast" for issue in issues)
    has_button_name = any(issue.get("violation", {}).get("id", "") == "button-name" for issue in issues)

    fallback_applied = False
    fallback_content = original_content

    if has_contrast:
        print("[React + Axe] Warning: contrast violations remain unchanged; applying a manual contrast fallback.")

        preferred_color = "\"#000\""
        for issue in issues:
            if issue.get("violation", {}).get("id", "") != "color-contrast":
                continue
            suggested_color = extract_contrast_payload(issue).get("suggestedColor") or "#000000"
            preferred_color = f'"{suggested_color}"'
            break

        def _tag_has_disabled_class(tag: str) -> bool:
            quoted_class_match = re.search(r'class(Name)?\s*=\s*["\']([^"\']+)["\']', tag)
            if quoted_class_match and "disabled" in quoted_class_match.group(2).split():
                return True

            expression_class_match = re.search(r'className\s*=\s*\{([^}]*)\}', tag)
            if expression_class_match and "disabled" in expression_class_match.group(1):
                return True

            return False

        def _upsert_react_style_props(tag: str, style_updates: Dict[str, str]) -> str:
            style_match = re.search(r'style\s*=\s*\{\{([\s\S]*?)\}\}', tag)
            if not style_match:
                declarations = ", ".join(f"{name}: {value}" for name, value in style_updates.items())
                return tag[:-1] + f' style={{{{ {declarations} }}}}>'

            style_body = style_match.group(1).strip()
            for name, value in style_updates.items():
                pattern = rf'\b{re.escape(name)}\s*:\s*([^,}}]+)'
                if re.search(pattern, style_body):
                    style_body = re.sub(pattern, f'{name}: {value}', style_body, count=1)
                else:
                    if style_body and not style_body.rstrip().endswith(','):
                        style_body = style_body.rstrip() + ','
                    style_body = f'{style_body} {name}: {value}'.strip()

            return tag[:style_match.start(1)] + style_body + tag[style_match.end(1):]

        def add_style(match):
            tag = match.group(0)
            style_updates = {"color": preferred_color}
            if _tag_has_disabled_class(tag):
                # Bootstrap's `.disabled` lowers opacity, which can keep computed contrast below 4.5:1.
                style_updates["opacity"] = "1"
            return _upsert_react_style_props(tag, style_updates)

        updated_parts: List[str] = []
        last_index = 0
        for start, end in _find_opening_tag_ranges(fallback_content, ("a", "button")):
            updated_parts.append(fallback_content[last_index:start])
            updated_parts.append(add_style(re.match(r'[\s\S]*', fallback_content[start:end])))
            last_index = end
        if updated_parts:
            updated_parts.append(fallback_content[last_index:])
            fallback_content = "".join(updated_parts)
        fallback_applied = True

    if has_button_name:
        print("[React + Axe] Warning: button-name violations remain unchanged; applying a manual aria-label fallback.")

        def add_aria_label(match):
            tag = match.group(0)
            if "aria-label" not in tag:
                return tag.replace('>', ' aria-label="Button">')
            return tag

        fallback_content = re.sub(r'<button([\s\S]*?)>\s*</button>', add_aria_label, fallback_content)
        fallback_applied = True

    if fallback_applied and fallback_content != original_content:
        print(f"[React + Axe] Manual fallback applied to {rel_path}")
        return fallback_content, True

    return original_content, False


def _request_react_component_fix(
    prompt: str,
    issues: List[Dict],
    screenshot_paths: Optional[List[str]],
    client,
) -> str:
    """Request a React component fix from the LLM and apply local post-processing."""
    system_message = (
        "You are an EXPERT in web accessibility (WCAG 2.2 A+AA) and React. "
        "Your MISSION is to fix ALL accessibility violations reported by Axe "
        "by modifying the full JSX component. "
        "🚨 CRITICAL: You MUST make real changes to the code. Do NOT return the same code. "
        "🚨 If there are contrast violations, you MUST add or modify style={{ color: '...' }} or color=\"...\" "
        "🚨 If there are aria-label, button-name, link-name violations, etc., you MUST add the required attributes. "
        "🚨 Keep React logic (hooks, props, state) intact. "
        "🚨 Do NOT change the responsive design - fixes must be visually invisible. "
        "🚨 For colour contrast, ONLY adjust text colour, do NOT change layout or backgrounds. "
        "🚨 If you return the same code unchanged, the fix FAILS completely. "
        "⚠️ IMPORTANT: If contrast errors are listed, you MUST change the colours. "
        "⚠️ If a Bootstrap-style disabled state or reduced opacity is causing the contrast failure, you MUST override the element opacity so the computed contrast passes while preserving the disabled semantics. "
        "⚠️ If the code already has a colour but Axe reports an error, it means: "
        "   a) The colour is not being applied correctly (add !important or use inline style), OR "
        "   b) A disabled/opacity style is altering the final rendered contrast, OR "
        "   c) You are changing the wrong element. "
        "⚠️ Find the EXACT element using the 'Affected HTML fragment' and make sure you change the correct colour. "
        "⚠️ Do NOT return the code unchanged if contrast violations are reported."
    )

    messages = [{"role": "system", "content": system_message}]
    has_contrast_errors = any(
        issue.get("violation", {}).get("id", "") == "color-contrast"
        for issue in issues
    )
    messages.append(
        _build_react_fix_user_message(
            prompt,
            screenshot_paths,
            has_contrast_errors,
        )
    )

    response = client.chat.completions.create(
        model="gpt-5",
        messages=messages,
    )

    corrected = response.choices[0].message.content or ""
    log_openai_call(
        prompt=prompt,
        response=corrected,
        model="gpt-5",
        call_type="react_axe_component_fix",
    )

    corrected = _normalize_react_llm_response(corrected)
    corrected = _apply_react_accessibility_fixes(corrected)
    corrected = _fix_basic_jsx_syntax_errors(corrected)
    corrected = _fix_react_aria_syntax(corrected)
    return corrected


def fix_react_components_with_axe_violations(
    issues_by_component: Dict[str, List[Dict]],
    project_root: Path,
    client,
    screenshot_paths: Optional[List[str]] = None,
    analysis_results_path: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
    """
    Use Axe information to ask the LLM to fix React components.
    
    This function is identical to Angular's fix_templates_with_axe_violations but for React.
    """
    fixes: Dict[str, Dict[str, str]] = {}
    failures: List[str] = []
    
    if not issues_by_component:
        print("[React + Axe] No violations were mapped to components.")
        return fixes

    color_catalog: List[Dict] = []
    if analysis_results_path:
        color_catalog = load_color_catalog(str(Path(analysis_results_path).with_name("color_catalog.json")))
    
    for rel_path, issues in issues_by_component.items():
        try:
            comp_path = project_root / rel_path
            if not comp_path.exists():
                failures.append(f"Component not found: {rel_path}")
                continue

            target_path = comp_path
            comp_source = comp_path.read_text(encoding="utf-8")
            if comp_path.suffix == ".ts":
                template_match = re.search(r'templateUrl\s*:\s*["\']([^"\']+)["\']', comp_source)
                if template_match:
                    candidate = (comp_path.parent / template_match.group(1)).resolve()
                    if candidate.exists() and candidate.suffix == ".html":
                        target_path = candidate

            original_content = target_path.read_text(encoding="utf-8")
            resolved_project_root = str(project_root.resolve())
            
            if not original_content.strip():
                continue

            contrast_issues, llm_issues = split_contrast_violations(issues)
            llm_issues_for_model = list(llm_issues)
            working_content = original_content
            source_repaired = 0
            if contrast_issues and color_catalog:
                remaining_contrast_issues: List[Dict] = []
                for issue in contrast_issues:
                    repair = apply_source_catalog_contrast_repair(issue, color_catalog, resolved_project_root)
                    if repair:
                        source_repaired += 1
                    else:
                        remaining_contrast_issues.append(issue)
                contrast_issues = remaining_contrast_issues
                if source_repaired:
                    print(
                        f"[React + Axe] Repaired {source_repaired} contrast issue(s) at source before the JSX fallback phase"
                    )

            if contrast_issues:
                working_content, fallback_applied = _apply_react_manual_fallbacks(
                    rel_path,
                    working_content,
                    contrast_issues,
                )
                if fallback_applied:
                    print(f"[React + Axe] Deterministically repaired {len(contrast_issues)} contrast issue(s) before the LLM phase")

                # Keep unresolved contrast issues in the LLM pass to maximize remediation coverage.
                llm_issues_for_model.extend(contrast_issues)

            if not llm_issues_for_model:
                if working_content != original_content or source_repaired:
                    target_path.write_text(working_content, encoding="utf-8")
                    fixes[rel_path] = {
                        "original": original_content,
                        "corrected": working_content,
                    }
                continue
            
            prompt_file = str(target_path.relative_to(project_root)) if project_root in target_path.parents else str(target_path)
            prompt = _build_axe_based_prompt_for_react_component(prompt_file, working_content, llm_issues_for_model)
            
            print(f"[React + Axe] Fixing component based on Axe: {rel_path}")
            print(f"[React + Axe] Violations to fix through LLM: {len(llm_issues_for_model)}")
            for i, issue in enumerate(llm_issues_for_model, 1):
                violation_id = issue.get("violation", {}).get("id", "unknown")
                print(f"  {i}. {violation_id}")

            corrected = _request_react_component_fix(
                prompt,
                llm_issues_for_model,
                screenshot_paths,
                client,
            )

            if contrast_issues:
                corrected, _ = _apply_react_manual_fallbacks(
                    rel_path,
                    corrected,
                    contrast_issues,
                )

            # CRITICAL VALIDATION: ensure LLM returned valid code (SAME AS ANGULAR)
            is_valid_response = _validate_react_llm_response(
                rel_path,
                working_content,
                corrected,
                allow_html_fragment=(target_path.suffix == ".html"),
            )
            diff_info = _detect_react_accessibility_changes(working_content, corrected)
            has_changes = diff_info["has_changes"]
            
            if is_valid_response and corrected and has_changes:
                if diff_info["has_color_diff"]:
                    print(f"[React + Axe] 🎨 Diferencia en colores detectada: {sorted(diff_info['orig_colors'])} -> {sorted(diff_info['corr_colors'])}")
                if diff_info["has_aria_diff"]:
                    print(f"[React + Axe] 🎨 Diferencia en ARIA detectada: {len(diff_info['orig_aria'])} -> {len(diff_info['corr_aria'])} atributos")
                if diff_info["has_alt_diff"]:
                    print(f"[React + Axe] 🎨 Diferencia en alt detectada: {len(diff_info['orig_alt'])} -> {len(diff_info['corr_alt'])} atributos")
                target_path.write_text(corrected, encoding="utf-8")
                fixes[rel_path] = {
                    "original": original_content,
                    "corrected": corrected,
                }
                print(f"[React + Axe] ✓ Cambios aplicados en {prompt_file}")
            else:
                if not is_valid_response:
                    print(f"[React + Axe] ⚠️ LLM returned invalid code for {rel_path}")
                else:
                    print(f"[React + Axe] ⚠️ LLM returned the same code for {rel_path}")
                fallback_content, fallback_applied = _apply_react_manual_fallbacks(
                    rel_path,
                    working_content,
                    llm_issues,
                )
                final_content = fallback_content if fallback_applied else working_content
                if final_content != original_content:
                    target_path.write_text(final_content, encoding="utf-8")
                    fixes[rel_path] = {
                        "original": original_content,
                        "corrected": final_content,
                    }

        except Exception as e:
            print(f"[React + Axe] ⚠️ Error fixing {rel_path}: {e}")
            failures.append(f"{rel_path}: {e}")

    if failures:
        raise RuntimeError(
            "React fix flow could not complete successfully for all components: "
            + "; ".join(failures)
        )
    
    return fixes


def _apply_react_accessibility_fixes(jsx_content: Optional[str]) -> Optional[str]:
    """Apply automatic accessibility fixes to JSX (same as Angular)."""
    if not jsx_content:
        return jsx_content
    
    corrected = jsx_content
    
    i_tags = re.finditer(r'<i\s+[^>]*aria-label=["\'][^"\']*["\'][^>]*>', corrected)
    for match in list(i_tags):
        tag = match.group(0)
        if 'role=' not in tag and 'role={' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    icon_tags = re.finditer(r'<Icon\s+[^>]*aria-label=["\'][^"\']*["\'][^>]*>', corrected)
    for match in list(icon_tags):
        tag = match.group(0)
        if 'role=' not in tag and 'role={' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)
    
    return corrected


def _fix_basic_jsx_syntax_errors(jsx_content: Optional[str]) -> Optional[str]:
    """Fix common basic JSX syntax errors (same as Angular but for JSX)."""
    if not jsx_content:
        return jsx_content
    
    corrected = jsx_content
    

    corrected = re.sub(
        r'style=\{\s*color:\s*([\'"])([^\'"]+)\1\s*\}',
        r"style={{ color: \1\2\1 }}",
        corrected
    )
    
    corrected = re.sub(
        r'style=\{\s*color:\s*([\'"])([^\'"]+)\1\1\s*\}',
        r"style={{ color: \1\2\1 }}",
        corrected
    )
    
    return corrected


def _fix_react_aria_syntax(jsx_content: Optional[str]) -> Optional[str]:
    """Fix ARIA attribute syntax in JSX."""
    if not jsx_content:
        return jsx_content
    return jsx_content
