"""
Angular per-component sandbox processing helpers.

This module exposes the component-processing boundary expected by
core.angular_handler while delegating prompt construction and static
analysis to narrower shared modules during the incremental refactor.
"""

import base64
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.angular_analysis import _analyze_template_for_accessibility_errors
from core.angular_prompts import _build_component_prompt
from core.angular_support import INLINE_TEMPLATE_PATTERNS, extract_inline_template
from core.contrast_engine import apply_manual_markup_contrast_repairs, split_contrast_violations
from core.angular_response_processing import (
    _apply_automatic_accessibility_fixes,
    _apply_automatic_contrast_fixes,
    _fix_angular_aria_syntax,
    _fix_basic_syntax_errors,
    _fix_responsive_breaking_changes,
    _parse_component_response,
)
from utils.io_utils import log_openai_call

ENABLE_AUTOMATIC_CONTRAST_FIXES = False

FORM_LABEL_RULE_IDS = {
    "label",
    "select-name",
    "aria-input-field-name",
    "aria-toggle-field-name",
    "input-button-name",
}


def _find_inline_template_match(ts_content: str) -> Optional[Dict[str, object]]:
    for pattern in INLINE_TEMPLATE_PATTERNS:
        match = pattern.search(ts_content)
        if not match:
            continue

        full_match = match.group(0)
        quote = "`"
        if "template:" in full_match and 'template: "' in full_match:
            quote = '"'
        elif "template:" in full_match and "template: '" in full_match:
            quote = "'"

        return {
            "quote": quote,
            "span": match.span("content"),
        }

    return None


def _escape_inline_template_content(template_content: str, quote: str) -> str:
    if quote == "`":
        return template_content

    escaped = template_content.replace("\\", "\\\\")
    escaped = escaped.replace(quote, f"\\{quote}")
    escaped = escaped.replace("\n", "\\n")
    return escaped


def _replace_inline_template(ts_content: str, inline_match: Dict[str, object], template_content: str) -> str:
    start, end = inline_match["span"]
    quote = str(inline_match["quote"])
    escaped_template = _escape_inline_template_content(template_content, quote)
    return ts_content[:start] + escaped_template + ts_content[end:]


def _build_template_change(
    template_path: Path,
    ts_path: Path,
    ts_content: Optional[str],
    corrected_template: str,
    template_metadata: Dict[str, object],
) -> Optional[Dict[str, str]]:
    if template_metadata.get("template_is_inline"):
        inline_match = template_metadata.get("inline_match")
        if not inline_match or ts_content is None:
            return None

        return {
            "path": str(ts_path),
            "original": ts_path.read_text(encoding="utf-8"),
            "corrected": _replace_inline_template(ts_content, inline_match, corrected_template),
        }

    return {
        "path": str(template_path),
        "original": template_path.read_text(encoding="utf-8"),
        "corrected": corrected_template,
    }


def _humanize_identifier(raw_value: str) -> str:
    text = (raw_value or "").strip()
    if not text:
        return ""
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1].upper() + text[1:] if text else ""


def _extract_label_from_form_field_block(field_block: str) -> str:
    placeholder_match = re.search(r'\bplaceholder\s*=\s*"([^"]+)"', field_block)
    if placeholder_match and placeholder_match.group(1).strip():
        return placeholder_match.group(1).strip()

    form_control_match = re.search(r'\bformControlName\s*=\s*"([^"]+)"', field_block)
    if form_control_match and form_control_match.group(1).strip():
        return _humanize_identifier(form_control_match.group(1).strip())

    return ""


def _ensure_aria_label_on_control(field_block: str, label_text: str) -> str:
    if not label_text:
        return field_block

    controls = ("input", "textarea", "mat-select")
    updated_block = field_block
    for control in controls:
        control_match = re.search(rf"<{control}\\b[^>]*>", updated_block)
        if not control_match:
            continue

        opening_tag = control_match.group(0)
        if re.search(r"\baria-label\s*=", opening_tag):
            return updated_block

        # Add aria-label to the first form control inside the mat-form-field.
        safe_label = label_text.replace('"', '&quot;')
        patched_tag = opening_tag[:-1] + f' aria-label="{safe_label}">'
        return updated_block.replace(opening_tag, patched_tag, 1)

    return updated_block


def apply_manual_form_label_repairs(
    template_content: str,
    axe_errors: List[Dict],
) -> Tuple[str, List[Dict]]:
    """Apply deterministic mat-form-field label repairs for common Axe form-name violations."""
    if not template_content:
        return template_content, []

    should_repair_forms = False
    for axe_error in axe_errors or []:
        violation = axe_error.get("violation", {}) or {}
        violation_id = axe_error.get("violation_id", violation.get("id", "unknown"))
        if str(violation_id).strip().lower() in FORM_LABEL_RULE_IDS:
            should_repair_forms = True
            break

    if not should_repair_forms:
        return template_content, []

    repaired_entries: List[Dict] = []

    def replace_form_field(match: re.Match) -> str:
        field_block = match.group(0)
        if re.search(r"<mat-label\\b", field_block):
            return field_block

        label_text = _extract_label_from_form_field_block(field_block)
        if not label_text:
            return field_block

        opening_tag_match = re.search(r"<mat-form-field\\b[^>]*>", field_block)
        if not opening_tag_match:
            return field_block

        opening_tag = opening_tag_match.group(0)
        repaired_block = field_block.replace(opening_tag, opening_tag + f"\n        <mat-label>{label_text}</mat-label>", 1)
        repaired_block = _ensure_aria_label_on_control(repaired_block, label_text)

        if repaired_block != field_block:
            repaired_entries.append({"type": "form-label", "label": label_text})

        return repaired_block

    updated_template = re.sub(
        r"<mat-form-field\\b[^>]*>.*?</mat-form-field>",
        replace_form_field,
        template_content,
        flags=re.DOTALL,
    )

    return updated_template, repaired_entries


def _load_component_sources(
    template_path: Path,
) -> Tuple[Path, Optional[Path], str, Optional[str], Optional[str], Dict[str, object]]:
    """Resolve sibling source files for a component template and load their contents."""
    component_dir = template_path.parent
    template_metadata: Dict[str, object] = {"template_is_inline": False, "inline_match": None}

    if template_path.suffix == ".ts":
        ts_path = template_path
        base_name = template_path.stem
    else:
        ts_path = component_dir / template_path.name.replace(".html", ".ts")
        base_name = template_path.name.replace(".html", "")

    styles_candidates = [
        component_dir / f"{base_name}.scss",
        component_dir / f"{base_name}.sass",
        component_dir / f"{base_name}.css",
    ]
    style_path = next((path for path in styles_candidates if path.exists()), None)

    ts_content = ts_path.read_text(encoding="utf-8") if ts_path.exists() else None
    if template_path.suffix == ".ts":
        template_content = extract_inline_template(ts_content or "") or ""
        template_metadata = {
            "template_is_inline": True,
            "inline_match": _find_inline_template_match(ts_content or ""),
        }
    else:
        template_content = template_path.read_text(encoding="utf-8")
    style_content = style_path.read_text(encoding="utf-8") if style_path and style_path.exists() else None
    return ts_path, style_path, template_content, ts_content, style_content, template_metadata


def _extract_contrast_info(node: Dict, violation_id: str) -> str:
    """Extract the most useful contrast metadata available from an Axe node."""
    if violation_id != "color-contrast":
        return ""

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
            return (
                f" | Text color: {fg_color}, Background color: {bg_color}, "
                f"Actual ratio: {ratio}, Required ratio: {expected_ratio}"
            )

    failure_summary = node.get("failureSummary", "")
    if failure_summary:
        ratio_match = re.search(r"contrast of ([\d.]+)", failure_summary, re.IGNORECASE)
        expected_match = re.search(
            r"Expected contrast ratio of ([\d.]+:?[\d]*)",
            failure_summary,
            re.IGNORECASE,
        )
        fg_match = re.search(r"foreground color: (#[0-9a-fA-F]+)", failure_summary, re.IGNORECASE)
        bg_match = re.search(r"background color: (#[0-9a-fA-F]+)", failure_summary, re.IGNORECASE)
        if ratio_match or expected_match:
            ratio_str = ratio_match.group(1) if ratio_match else "N/A"
            expected_str = expected_match.group(1) if expected_match else "4.5:1"
            fg_str = fg_match.group(1) if fg_match else "N/A"
            bg_str = bg_match.group(1) if bg_match else "N/A"
            return (
                f" | Text color: {fg_str}, Background color: {bg_str}, "
                f"Actual ratio: {ratio_str}, Required ratio: {expected_str}"
            )

    for check in checks:
        message = check.get("message", "")
        if "contrast" not in message.lower() or (
            "insufficient" not in message.lower() and "ratio" not in message.lower()
        ):
            continue

        ratio_match = re.search(r"contrast of ([\d.]+)", message, re.IGNORECASE)
        expected_match = re.search(
            r"Expected contrast ratio of ([\d.]+:?[\d]*)",
            message,
            re.IGNORECASE,
        )
        fg_match = re.search(r"foreground color: (#[0-9a-fA-F]+)", message, re.IGNORECASE)
        bg_match = re.search(r"background color: (#[0-9a-fA-F]+)", message, re.IGNORECASE)
        if ratio_match:
            ratio_str = ratio_match.group(1)
            expected_str = expected_match.group(1) if expected_match else "4.5:1"
            fg_str = fg_match.group(1) if fg_match else "N/A"
            bg_str = bg_match.group(1) if bg_match else "N/A"
            return (
                f" | Text color: {fg_str}, Background color: {bg_str}, "
                f"Actual ratio: {ratio_str}, Required ratio: {expected_str}"
            )

    return ""


def _format_axe_error_message(axe_error: Dict) -> str:
    """Normalize an Axe error into the textual format consumed by the prompt builder."""
    violation = axe_error.get("violation", {})
    node = axe_error.get("node", {})
    violation_id = axe_error.get("violation_id", violation.get("id", "unknown"))
    targets = node.get("target", [])
    selector = targets[0] if targets and isinstance(targets[0], str) else "No selector"
    html_snippet = (node.get("html") or "").strip()
    html_display = html_snippet[:200] if html_snippet else ""
    description = violation.get("description", "")
    help_text = violation.get("help", "")
    contrast_info = _extract_contrast_info(node, violation_id)

    error_parts = [f"ERROR AXE: {violation_id}"]
    if selector and selector != "No selector":
        error_parts.append(f"Selector CSS: {selector}")
        if ".mdc-button__label" in selector or ".mat-button-label" in selector or " > " in selector:
            parent_selector = selector.split(" > ")[0] if " > " in selector else selector.replace(".mdc-button__label", "").strip()
            error_parts.append(
                "⚠️ NOTE: This selector targets an internal element generated by Angular Material. "
                f"Find the PARENT element in the template (e.g. button with {parent_selector}) and apply the style there."
            )

    if description:
        error_parts.append(f"Description: {description}")
    if contrast_info:
        error_parts.append(f"Contrast data: {contrast_info.strip()}")

    if html_display:
        clean_html = re.sub(r'\s+_ngcontent-[^=]*="[^"]*"', "", html_display)
        clean_html = re.sub(r'\s+_nghost-[^=]*="[^"]*"', "", clean_html)
        error_parts.append(f"Affected HTML: {clean_html}")
        if "mdc-button__label" in clean_html or "mat-button-label" in clean_html:
            text_match = re.search(r">\s*([^<]+)\s*<", clean_html)
            if text_match:
                button_text = text_match.group(1).strip()
                error_parts.append(
                    "⚠️ NOTE: This span is generated by Angular Material. "
                    f"Find the button that contains the text '{button_text}' in the template."
                )

    if help_text:
        error_parts.append(f"Help: {help_text}")

    return " | ".join(error_parts)


def process_single_component_sandbox(
    template_path: Path,
    client,
    project_root: Path,
    axe_errors: List[Dict] = None,
    screenshot_paths: List[str] = None,
) -> Tuple[Dict, Optional[Dict]]:
    """Process a component in sandbox mode and return a change map."""
    base_component_name = template_path.stem.replace(".component", "")
    ts_path, style_path, template_content, ts_content, style_content, template_metadata = _load_component_sources(template_path)
    original_template_content = template_content

    contrast_repairs: List[Dict] = []
    llm_axe_errors = axe_errors or []
    if axe_errors:
        contrast_axe_errors, non_contrast_axe_errors = split_contrast_violations(axe_errors)
        llm_axe_errors = list(non_contrast_axe_errors)
        if contrast_axe_errors:
            template_content, contrast_repairs = apply_manual_markup_contrast_repairs(
                template_content,
                contrast_axe_errors,
            )
            if contrast_repairs:
                print(f"  → Deterministically repaired {len(contrast_repairs)} contrast issue(s) before the LLM phase")

            # Keep contrast violations in the LLM pass to catch unresolved cases.
            llm_axe_errors.extend(contrast_axe_errors)

    detected_errors = _analyze_template_for_accessibility_errors(template_content, style_content)
    if llm_axe_errors:
        print(f"  → {len(llm_axe_errors)} Axe error(s) detected for this component")
        for axe_error in llm_axe_errors:
            detected_errors.append(_format_axe_error_message(axe_error))

    if detected_errors:
        print(f"  → Detected {len(detected_errors)} accessibility issue(s) in {base_component_name}")
        for error in detected_errors[:5]:
            print(f"    - {error[:80]}")
    else:
        print(f"  → No obvious errors detected in {base_component_name} (LLM should look deeper)")

    if contrast_repairs and not llm_axe_errors and not detected_errors:
        template_change = _build_template_change(
            template_path,
            ts_path,
            ts_content,
            template_content,
            template_metadata,
        )
        if template_change:
            print("  → Persisting deterministic contrast-only repair without invoking the LLM")
            return {
                "component_name": base_component_name,
                "template_path": str(template_path),
                "typescript_path": str(ts_path) if ts_path.exists() else None,
                "styles_path": str(style_path) if style_path else None,
                "status": "updated",
                "changes": {
                    "template": True,
                    "typescript": False,
                    "styles": False,
                },
            }, {"template": template_change}

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

    contrast_errors = [error for error in detected_errors if "contrast" in error.lower()]
    if contrast_errors:
        print(f"  → {len(contrast_errors)} static contrast warning(s) remain after deterministic repairs")

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

    messages = [{"role": "system", "content": system_message}]

    if screenshot_paths:
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
   - Fixes should be visually \"invisible\"
   - Use aria-label, roles, alt text, and minimal contrast adjustments
   - The final design must look IDENTICAL to the screenshots, but accessible

The screenshots show the application BEFORE the fixes. Your job is to make it accessible while keeping that exact visual appearance.
"""
        user_content = [{"type": "text", "text": user_prompt + screenshot_instructions}]
        for screenshot_path in screenshot_paths:
            try:
                screenshot_file = Path(screenshot_path)
                if screenshot_file.exists():
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
                print(f"  Warning: failed to include screenshot {screenshot_path}: {exc}")
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": user_prompt})

    response = client.chat.completions.create(
        model="gpt-5",
        messages=messages,
    )

    response_text = response.choices[0].message.content or ""
    log_openai_call(prompt=user_prompt, response=response_text, model="gpt-5", call_type="angular_component_fix")

    print(f"  → LLM responded with {len(response_text)} characters")

    try:
        parsed_response = _parse_component_response(response_text)
        print("  → Response parsed successfully")
    except Exception as exc:
        print(f"  Error parsing the LLM response: {exc}")
        template_match = re.search(r"<<<TEMPLATE>>>\s*(.*?)\s*<<<END TEMPLATE>>>", response_text, re.DOTALL)
        if template_match:
            parsed_response = {
                "template": template_match.group(1).strip(),
                "typescript": None,
                "styles": None,
            }
            print("  → Template extracted using a fallback regex")
        else:
            print("  Error: could not extract a template from the response.")
            return {
                "component_name": base_component_name,
                "template_path": str(template_path),
                "typescript_path": str(ts_path) if ts_path.exists() else None,
                "styles_path": str(style_path) if style_path else None,
                "status": "error",
                "error": f"Error parseando respuesta: {exc}",
                "changes": {},
            }, None

    template_content_corrected = _fix_angular_aria_syntax(parsed_response.get("template"))
    template_content_corrected = _fix_basic_syntax_errors(template_content_corrected)
    template_content_corrected = _apply_automatic_accessibility_fixes(template_content_corrected)
    template_content_corrected = _fix_responsive_breaking_changes(template_content, template_content_corrected)

    if not template_content_corrected:
        print("  Warning: no corrected template was returned by the LLM.")
        return {
            "component_name": base_component_name,
            "template_path": str(template_path),
            "typescript_path": str(ts_path) if ts_path.exists() else None,
            "styles_path": str(style_path) if style_path else None,
            "status": "error",
            "error": "No corrected template was returned",
            "changes": {},
        }, None

    if contrast_errors and ENABLE_AUTOMATIC_CONTRAST_FIXES:
        print(f"  → Applying automatic fixes for {len(contrast_errors)} detected contrast errors")
        template_content_corrected = _apply_automatic_contrast_fixes(template_content_corrected, contrast_errors)

    print(
        f"  → Corrected template length: {len(template_content_corrected)} characters "
        f"(original: {len(template_content)} characters)"
    )

    original_clean = "\n".join(line.rstrip() for line in original_template_content.split("\n"))
    corrected_clean = "\n".join(line.rstrip() for line in template_content_corrected.split("\n"))
    changes: Dict[str, Dict[str, str]] = {}

    are_different = (
        original_clean.strip() != corrected_clean.strip()
        or len(original_clean.strip()) != len(corrected_clean.strip())
        or template_content.strip() != template_content_corrected.strip()
    )

    if detected_errors and not are_different:
        print(
            f"  ⚠️ No differences detected in comparison, but there are {len(detected_errors)} automatically detected errors"
        )
        print("  → Forcing application of changes because there are errors that must be fixed")
        are_different = True

    if not are_different:
        print("  ⚠️ Corrected template appears IDENTICAL to original")
        print("  → Comparing lines...")
        original_lines = template_content.strip().split("\n")
        corrected_lines = template_content_corrected.strip().split("\n")
        if len(original_lines) != len(corrected_lines):
            print(f"    → Different line count: {len(original_lines)} vs {len(corrected_lines)}")
            are_different = True
        else:
            print(f"    → Same line count: {len(original_lines)}")
            differences_found = False
            for index, (original_line, corrected_line) in enumerate(zip(original_lines, corrected_lines)):
                if original_line.strip() != corrected_line.strip():
                    print(f"    → Difference at line {index + 1}:")
                    print(f"      Original: {original_line[:100]}")
                    print(f"      Corrected: {corrected_line[:100]}")
                    differences_found = True
                    are_different = True
                    break
            if not differences_found:
                print("    → No line-by-line differences found")
                if detected_errors:
                    print(f"    → BUT there are {len(detected_errors)} detected errors, forcing application of changes")
                    are_different = True

    if are_different:
        print(f"  ✓ Changes detected in template of {base_component_name}")
        print(
            f"    → Original: {len(original_clean.strip())} chars, "
            f"Corrected: {len(corrected_clean.strip())} chars"
        )
        template_change = _build_template_change(
            template_path,
            ts_path,
            ts_content,
            template_content_corrected,
            template_metadata,
        )
        if template_change:
            changes["template"] = template_change
    else:
        print(f"  ⚠️ No changes detected in template of {base_component_name}")
        print("    → LLM returned the same code. This indicates that:")
        print("      1. The LLM did not detect accessibility errors")
        print("      2. The LLM detected errors but did not fix them")
        print("      3. The template really has no errors (unlikely)")
        if detected_errors:
            print(f"    → {len(detected_errors)} errors were detected automatically, but the LLM did not fix them")
            for error in detected_errors[:5]:
                print(f"      - {error[:80]}")
            print("    → FORCING application of changes because errors were detected")
            template_change = _build_template_change(
                template_path,
                ts_path,
                ts_content,
                template_content_corrected,
                template_metadata,
            )
            if template_change:
                changes["template"] = template_change

    if ts_content is not None and not template_metadata.get("template_is_inline"):
        ts_corrected = parsed_response.get("typescript")
        if ts_corrected and ts_corrected.strip() != ts_content.strip():
            changes["typescript"] = {
                "path": str(ts_path),
                "original": ts_content,
                "corrected": ts_corrected,
            }

    if style_path and style_content is not None:
        style_corrected = parsed_response.get("styles")
        if style_corrected and style_corrected.strip() != style_content.strip():
            changes["styles"] = {
                "path": str(style_path),
                "original": style_content,
                "corrected": style_corrected,
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


def apply_changes_map(changes_map: List[Dict]) -> int:
    """Apply the change map to the actual source files."""
    applied_count = 0
    for change_entry in changes_map:
        changes = change_entry.get("changes", {})
        for file_change in changes.values():
            try:
                target_path = Path(file_change["path"])
                target_path.write_text(file_change["corrected"], encoding="utf-8")
                applied_count += 1
            except Exception as exc:
                print(f"  ⚠️ Error aplicando cambio en {file_change['path']}: {exc}")
    return applied_count
