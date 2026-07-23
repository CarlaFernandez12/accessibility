"""
Accessible HTML generation and post‑processing helpers.

This module is responsible for:
    - Guiding colour contrast corrections.
    - Coordinating prompt construction for the LLM.
    - Applying fragment‑level fixes back into the DOM.
    - Performing a final responsive merge while preserving accessibility fixes.

Business logic and behaviour must remain stable; refactors here focus on
clarity, documentation and type hints only.
"""

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from config.constants import OPENAI_MODEL
from core.contrast_engine import apply_manual_contrast_fix, split_contrast_violations
from core.html_dom import (
    _apply_corrected_node_with_fallbacks,
    _find_node_by_html_snippet,
    _find_node_by_selector,
    _locate_node_to_fix,
    _normalize_angular_html,
)
from core.html_heuristics import (
    _ensure_discernible_buttons,
    _ensure_discernible_links,
    _get_fragment_images,
    _process_image_descriptions,
)
from core.html_prompting import (
    build_general_prompt,
    build_responsive_prompt,
    extract_clean_html,
    validate_responsive_html,
)

from utils.html_utils import convert_paths_to_absolute
from utils.io_utils import log_openai_call
from utils.violation_utils import flatten_violations, prioritize_violations

def _build_llm_violation_directives(violation: Dict[str, Any]) -> str:
    """Return strict, violation-specific rules to improve LLM correction reliability."""
    violation_id = (violation.get("violation_id") or "").lower()
    failure_summary = (violation.get("failure_summary") or "").strip()

    common = [
        "MUST return only the corrected fragment for this exact node.",
        "MUST keep the same root tag and preserve framework syntax (*ngIf, [attr], (click), {{ }}).",
        "MUST make a real accessibility change; do not return the original fragment unchanged.",
    ]

    specific: List[str] = []
    if violation_id == "button-name":
        specific.append("If the control has no accessible name, add aria-label or aria-labelledby with meaningful text.")
    elif violation_id == "label":
        specific.append("For input/select/textarea without label, add a programmatic label via aria-label or aria-labelledby.")
        specific.append("Do not remove existing placeholders, bindings, validators, or control attributes.")
    elif violation_id.startswith("aria-"):
        specific.append("Fix invalid/missing ARIA attributes according to WAI-ARIA semantics for this element.")
        specific.append("Do not invent non-standard aria-* attributes.")

    if failure_summary:
        specific.append(f"Axe failure summary to satisfy: {failure_summary}")

    return "\n".join(common + specific)


def _violation_still_present(soup, node_to_fix, violation: Dict[str, Any]) -> bool:
    """Check whether a subset of high-noise violations still exists on the current node."""
    violation_id = (violation.get("violation_id") or "").lower()

    if violation_id == "button-name":
        is_button_like = node_to_fix.name == "button" or (node_to_fix.get("role") or "").strip().lower() == "button"
        if not is_button_like:
            return False
        has_name = (node_to_fix.get_text() or "").strip() or (node_to_fix.get("aria-label") or "").strip() or (node_to_fix.get("aria-labelledby") or "").strip()
        return not bool(has_name)

    if violation_id == "label":
        if node_to_fix.name not in {"input", "select", "textarea"}:
            return False
        if (node_to_fix.get("type") or "").lower() == "hidden":
            return False

        has_programmatic_label = (node_to_fix.get("aria-label") or "").strip() or (node_to_fix.get("aria-labelledby") or "").strip()
        if has_programmatic_label:
            return False

        field_id = (node_to_fix.get("id") or "").strip()
        if field_id:
            explicit_label = soup.find("label", attrs={"for": field_id})
            if explicit_label and (explicit_label.get_text() or "").strip():
                return False
        return True

    if violation_id.startswith("aria-"):
        if "aria-valid-attr" in violation_id or "aria-allowed-attr" in violation_id:
            # We cannot reliably infer invalid aria-* names without full Axe rule context,
            # so keep this violation eligible for post-merge LLM re-check.
            return True
        if "aria-required-attr" in violation_id:
            is_interactive = node_to_fix.name in {"button", "input", "select", "textarea", "a"} or (node_to_fix.get("role") or "") == "button"
            if is_interactive:
                has_name = (node_to_fix.get_text() or "").strip() or (node_to_fix.get("aria-label") or "").strip() or (node_to_fix.get("aria-labelledby") or "").strip()
                return not bool(has_name)

    return True


def _infer_control_label(node) -> str:
    """Infer a conservative accessible name for interactive controls."""
    for attr_name in ("aria-label", "title", "placeholder", "name", "id"):
        value = (node.get(attr_name) or "").strip()
        if value:
            return value.replace("_", " ").replace("-", " ").strip().title()

    text_value = (node.get_text() or "").strip()
    if text_value:
        return text_value

    class_names = " ".join(node.get("class", []) if isinstance(node.get("class", []), list) else str(node.get("class", "")).split()).lower()
    icon_value = (node.get("icon") or "").strip().lower()
    joined = f"{class_names} {icon_value}"
    if "search" in joined:
        return "Search"
    if "close" in joined:
        return "Close"
    if "next" in joined:
        return "Next"
    if "prev" in joined:
        return "Previous"
    if node.name == "button" or (node.get("role") or "").strip().lower() == "button":
        return "Button"
    return "Input"


def _apply_non_destructive_a11y_safety_net(soup) -> int:
    """Apply a minimal fallback for unresolved accessible-name issues after LLM passes."""
    applied = 0

    # Button-like controls without discernible name.
    buttons = set(soup.find_all("button"))
    buttons.update(soup.find_all(attrs={"role": "button"}))
    for btn in buttons:
        has_name = (btn.get_text() or "").strip() or (btn.get("aria-label") or "").strip() or (btn.get("aria-labelledby") or "").strip()
        if has_name:
            continue
        btn["aria-label"] = _infer_control_label(btn)
        applied += 1

    # Form controls without explicit/programmatic label.
    for control in soup.find_all(["input", "select", "textarea"]):
        if (control.get("type") or "").lower() == "hidden":
            continue

        has_programmatic_label = (control.get("aria-label") or "").strip() or (control.get("aria-labelledby") or "").strip()
        if has_programmatic_label:
            continue

        control_id = (control.get("id") or "").strip()
        if control_id:
            label_node = soup.find("label", attrs={"for": control_id})
            if label_node and (label_node.get_text() or "").strip():
                continue

        control["aria-label"] = _infer_control_label(control)
        applied += 1

    # Custom input-like controls (e.g., j-input) lacking role/name semantics.
    for node in soup.find_all(True):
        if "-" not in node.name:
            continue
        is_input_like = node.name.endswith("input") or "input" in node.name or (node.get("icon") or "").strip().lower() == "search"
        if not is_input_like:
            continue

        if not (node.get("aria-label") or "").strip():
            node["aria-label"] = _infer_control_label(node)
            applied += 1

        if not (node.get("role") or "").strip():
            node["role"] = "searchbox" if "search" in ((node.get("icon") or "").strip().lower() + " " + (node.get("aria-label") or "").strip().lower()) else "textbox"
            applied += 1

    return applied




def _call_llm_for_fix(client, prompt, system_message, screenshot_paths=None):
    """Call the LLM to correct an HTML fragment."""
    messages = [
        {"role": "system", "content": system_message}, 
    ]

    if screenshot_paths:
        import base64
        user_content = [{"type": "text", "text": prompt}]
        for screenshot_path in screenshot_paths:
            try:
                from pathlib import Path
                screenshot_file = Path(screenshot_path)
                if screenshot_file.exists():
                    with open(screenshot_file, "rb") as img_file:
                        image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
                        mime_type = "image/png"
                        if screenshot_path.endswith('.jpg') or screenshot_path.endswith('.jpeg'):
                            mime_type = "image/jpeg"
                        user_content.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            }
                        })
            except Exception as e:
                print(f"  Warning: failed to include screenshot {screenshot_path}: {e}")
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": prompt})
    
    response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages
            )
    return extract_clean_html(response.choices[0].message.content)

def _load_color_catalog(search_root: str = ".") -> List[Dict[str, Any]]:
    """Load the nearest available color catalog from common result locations."""
    color_catalog_path = None

    for candidate in ["color_catalog.json", "../color_catalog.json"]:
        if os.path.exists(candidate):
            color_catalog_path = candidate
            break

    if not color_catalog_path:
        for root, _, files in os.walk(search_root):
            for filename in files:
                if filename == "color_catalog.json":
                    color_catalog_path = os.path.join(root, filename)
                    break
            if color_catalog_path:
                break

    if not color_catalog_path or not os.path.exists(color_catalog_path):
        return []

    try:
        with open(color_catalog_path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return []


def _parse_llm_html_fragment(response_content: str):
    """Normalize an LLM HTML response and parse its first corrected node when possible."""
    cleaned_response = response_content.strip()
    if cleaned_response.startswith("```"):
        parts = cleaned_response.split("```")
        if len(parts) >= 3:
            code_block = parts[1]
            if "\n" in code_block:
                code_block = code_block.split("\n", 1)[1]
            cleaned_response = code_block.strip()
        else:
            cleaned_response = cleaned_response.replace("```html", "").replace("```", "").strip()

    new_node = None
    try:
        parsed_soup = BeautifulSoup(cleaned_response, 'html.parser')
        new_node = parsed_soup.find()
    except Exception as parse_error:
        print(f"    Warning: failed to parse LLM response: {parse_error}")
        try:
            tag_match = re.search(r'<[a-zA-Z][^>]*>.*?</[a-zA-Z]+>', cleaned_response, re.DOTALL)
            if tag_match:
                cleaned_response = tag_match.group(0)
                parsed_soup = BeautifulSoup(cleaned_response, 'html.parser')
                new_node = parsed_soup.find()
        except Exception:
            pass

    return cleaned_response, new_node


def _build_responsive_user_message(responsive_prompt: str, screenshot_paths=None) -> Dict[str, Any]:
    """Build the responsive-merge user message, optionally embedding screenshots."""
    has_screenshots = screenshot_paths is not None and len(screenshot_paths) > 0
    if not has_screenshots:
        return {"role": "user", "content": responsive_prompt}

    user_content = [{"type": "text", "text": responsive_prompt}]
    for screenshot_path in screenshot_paths:
        try:
            screenshot_file = Path(screenshot_path)
            if not screenshot_file.exists():
                continue
            with open(screenshot_file, "rb") as img_file:
                image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
            mime_type = "image/png"
            if screenshot_path.endswith('.jpg') or screenshot_path.endswith('.jpeg'):
                mime_type = "image/jpeg"
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{image_base64}"
                }
            })
        except Exception as exc:
            print(f"  Warning: failed to include screenshot {screenshot_path}: {exc}")

    return {"role": "user", "content": user_content}


def _restore_responsive_design(original_html: str, soup, client, screenshot_paths=None):
    """Restore responsive layout while preserving the accessibility fixes already applied to the soup."""
    _ensure_discernible_buttons(soup)
    _ensure_discernible_links(soup)
    current_html = str(soup)

    estimated_tokens = len(original_html) / 4 + len(current_html) / 4
    if estimated_tokens > 100000:
        print(f"  Warning: HTML is too large ({estimated_tokens:.0f} estimated tokens); skipping responsive merge.")
        print("  Using the corrected HTML directly.")
        return soup

    has_screenshots = screenshot_paths is not None and len(screenshot_paths) > 0
    responsive_prompt = build_responsive_prompt(original_html, current_html, has_screenshots)
    responsive_system = "You are a responsive design expert. MERGE element by element: combine the layout CSS properties from the original HTML with ALL accessibility attributes from the current HTML. NEVER remove aria-*, alt, title, lang, labels, or contrast styles (color, background-color). CRITICAL: Contrast styles (style with 'color:' or 'background-color:') in the CURRENT HTML MUST be preserved COMPLETELY. If an element has contrast styles in the CURRENT one, keep those styles and add the layout styles from the ORIGINAL. The result must have the original's responsive design + all accessibility fixes. CRITICAL: Keep ALL HTML content, including footer, scripts at the end, and any bottom elements. Do NOT remove any part of the HTML. If screenshots are available, the final design MUST look IDENTICAL to the screenshots in terms of layout, sizes, spacing and background colours."

    try:
        messages = [
            {"role": "system", "content": responsive_system},
            _build_responsive_user_message(responsive_prompt, screenshot_paths),
        ]

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_completion_tokens=200000
        )

        responsive_html = extract_clean_html(response.choices[0].message.content)
        validated_soup = validate_responsive_html(responsive_html, original_html, current_html)

        if validated_soup:
            print("  Responsive design restored while preserving accessibility fixes.")
            return validated_soup

        print("  Using the current HTML instead of the responsive merge result.")
        return soup
    except Exception as api_error:
        error_str = str(api_error)
        if "context_length_exceeded" in error_str or "maximum context length" in error_str:
            print("  Warning: HTML is too large for the model; skipping responsive merge.")
            print("  Using the corrected HTML directly.")
            return soup
        raise


def _harden_post_merge_accessibility_fixes(soup, contrast_violations):
    """Re-apply accessibility fixes after responsive merging can strip them."""
    _ensure_discernible_buttons(soup)
    _ensure_discernible_links(soup)

    for violation in contrast_violations:
        selector = violation.get('selector', '')
        html_snippet = violation.get('html_snippet', '')
        node_to_fix = _locate_node_to_fix(soup, selector, html_snippet)
        if node_to_fix:
            apply_manual_contrast_fix(node_to_fix, violation)

    return soup

def generate_accessible_html_with_parser(
    original_html,
    axe_results,
    media_descriptions,
    client,
    base_url,
    driver,
    screenshot_paths=None,
    convert_relative_paths: bool = True,
):
    color_catalog = _load_color_catalog()

    print("\n--- Starting LLM-only correction process ---")

    soup = BeautifulSoup(original_html, 'html.parser')
    all_violations = flatten_violations(axe_results.get('violations', []))

    if not all_violations:
        print("No actionable violations were found.")
        return original_html

    print("\n[Phase 1/3] Processing violations...")
    violations_to_fix = [v for v in all_violations if v.get('selector') and v.get('selector') != 'No selector']
    violations_to_fix = prioritize_violations(violations_to_fix)
    contrast_violations, violations_to_fix = split_contrast_violations(violations_to_fix)

    _process_image_descriptions(soup, media_descriptions, base_url)

    manually_repaired_contrast = 0
    unresolved_contrast_violations = []
    if contrast_violations:
        print(f"  Applying deterministic contrast repair to {len(contrast_violations)} violation(s) before the LLM phase.")
        for violation in contrast_violations:
            selector = violation.get('selector', '')
            html_snippet = violation.get('html_snippet', '')
            node_to_fix = _locate_node_to_fix(soup, selector, html_snippet)
            if not node_to_fix:
                unresolved_contrast_violations.append(violation)
                continue
            repair_result = apply_manual_contrast_fix(node_to_fix, violation)
            if repair_result:
                manually_repaired_contrast += 1
            else:
                unresolved_contrast_violations.append(violation)
        print(f"  Deterministic contrast repairs applied: {manually_repaired_contrast}")

    if unresolved_contrast_violations:
        print(
            f"  Deterministic repair could not resolve {len(unresolved_contrast_violations)} contrast violation(s); sending them to the LLM phase."
        )
        violations_to_fix = unresolved_contrast_violations + violations_to_fix

    print(f"\n[Phase 2/3] Fixing {len(violations_to_fix)} visible violations...")

    successful_fixes = 0
    failed_fixes = 0
    failed_violations: List[Dict[str, Any]] = []

    print(f"  Processing {len(violations_to_fix)} violations in total.")

    violation_types = {}
    for v in violations_to_fix:
        v_id = v.get('violation_id', 'unknown')
        violation_types[v_id] = violation_types.get(v_id, 0) + 1

    if violation_types:
        print("  Violation types detected:")
        for v_type, count in sorted(violation_types.items(), key=lambda x: x[1], reverse=True):
            print(f"     - {v_type}: {count} violation(s)")

    for violation in violations_to_fix:
        try:
            selector = violation.get('selector', '')
            html_snippet = violation.get('html_snippet', '')
            violation_id = violation.get('violation_id', 'unknown')

            node_to_fix = _locate_node_to_fix(soup, selector, html_snippet)
            if not node_to_fix:
                print(f"  Warning: no element found for selector: {selector[:50]}... (after fallback matching)")
                print(f"     Full selector: {selector[:150]}")
                if html_snippet:
                    print(f"     HTML snippet: {html_snippet[:100]}...")
                failed_fixes += 1
                failed_violations.append(violation)
                continue
            
            violation_id = violation.get('violation_id', 'unknown')
            impact = violation.get('impact', 'moderate')

            print(f"  Fixing '{selector}' for '{violation_id}' (impact: {impact})")
            
            original_fragment = str(node_to_fix)
            images_info = _get_fragment_images(original_fragment, media_descriptions, base_url)
            
            has_screenshots = screenshot_paths is not None and len(screenshot_paths) > 0

            base_prompt = build_general_prompt(violation, original_fragment, images_info, has_screenshots)
            directives = _build_llm_violation_directives(violation)
            prompt = f"{base_prompt}\n\nMandatory correction rules:\n{directives}"
            system_message = "You are a web accessibility expert. Your PRIORITY is to fix ALL mentioned accessibility errors while KEEPING the responsive design shown in the screenshots. Fixes should be visually 'invisible' (use aria-label, roles, alt text). Do NOT add HTML comments or attributes that show they were fixes. The HTML should look like original code, not corrected. Return only the corrected fragment."

            fixed_with_llm = False
            last_parse_preview = ""
            retry_prompt = prompt
            for attempt in range(1, 4):
                corrected_fragment_str = _call_llm_for_fix(client, retry_prompt, system_message, screenshot_paths)
                log_openai_call(
                    prompt=retry_prompt,
                    response=corrected_fragment_str,
                    model=OPENAI_MODEL,
                    call_type=f"html_fix_attempt_{attempt}",
                )

                if not corrected_fragment_str:
                    print(f"    Warning: empty LLM response on attempt {attempt}.")
                    retry_prompt = (
                        f"{prompt}\n\nPrevious response was empty. You MUST return corrected HTML for this exact node."
                    )
                    continue

                cleaned_response, new_node = _parse_llm_html_fragment(corrected_fragment_str)
                last_parse_preview = cleaned_response[:200]
                if not new_node:
                    print(f"    Warning: could not parse LLM correction on attempt {attempt}.")
                    retry_prompt = (
                        f"{prompt}\n\nPrevious response was not valid HTML. Return ONLY one corrected HTML fragment."
                    )
                    continue

                original_str = str(node_to_fix).strip()
                new_str = str(new_node).strip()
                original_normalized = _normalize_angular_html(original_str)
                new_normalized = _normalize_angular_html(new_str)

                if original_normalized.strip() == new_normalized.strip():
                    print(f"    Warning: unchanged fragment on attempt {attempt}; retrying with stricter instructions.")
                    retry_prompt = (
                        f"{prompt}\n\nYour previous output was identical. Apply a concrete fix for '{violation_id}' now."
                    )
                    continue

                replaced = _apply_corrected_node_with_fallbacks(
                    soup,
                    selector,
                    html_snippet,
                    new_node,
                    original_normalized,
                )
                if replaced:
                    successful_fixes += 1
                    fixed_with_llm = True
                    break

                print(f"    Warning: could not apply corrected node on attempt {attempt}; retrying.")
                retry_prompt = (
                    f"{prompt}\n\nThe fragment could not be applied. Keep the same root element and attributes structure."
                )

            if not fixed_with_llm:
                failed_fixes += 1
                failed_violations.append(violation)
                if last_parse_preview:
                    print("    Error: failed to apply LLM correction after retries.")
                    print(f"       Response preview: {last_parse_preview}...")
                else:
                    print("    Error: failed to get a usable LLM correction after retries.")
            
        except Exception as e:
            failed_fixes += 1
            failed_violations.append(violation)
            print(f"  Error while processing '{violation.get('selector', '')}': {e}")
    
    print(f"\n[Summary] Successful fixes: {successful_fixes}, failed fixes: {failed_fixes}")
    
    print(f"\n[Phase 3/3] Restoring responsive design while keeping accessibility fixes...")
    
    try:
        soup = _restore_responsive_design(original_html, soup, client, screenshot_paths)
    except Exception as e:
        print(f"  Warning: responsive merge failed: {e}. Continuing with the current HTML.")

    soup = _harden_post_merge_accessibility_fixes(soup, contrast_violations)

    print("\n[Phase 4/4] Verifying post-merge accessibility fixes...")
    post_merge_targets = [
        v
        for v in violations_to_fix
        if (v.get("violation_id") or "").lower() == "button-name"
        or (v.get("violation_id") or "").lower() == "label"
        or (v.get("violation_id") or "").lower().startswith("aria-")
    ]
    post_merge_refixed = 0

    for violation in post_merge_targets:
        selector = violation.get("selector", "")
        html_snippet = violation.get("html_snippet", "")
        node_to_fix = _locate_node_to_fix(soup, selector, html_snippet)
        if not node_to_fix:
            continue
        if not _violation_still_present(soup, node_to_fix, violation):
            continue

        original_fragment = str(node_to_fix)
        images_info = _get_fragment_images(original_fragment, media_descriptions, base_url)
        has_screenshots = screenshot_paths is not None and len(screenshot_paths) > 0
        base_prompt = build_general_prompt(violation, original_fragment, images_info, has_screenshots)
        directives = _build_llm_violation_directives(violation)
        prompt = (
            f"{base_prompt}\n\nMandatory correction rules:\n{directives}"
            "\n\nThis is a POST-MERGE revalidation pass. The violation still exists and MUST be fixed now."
        )
        system_message = "You are a web accessibility expert. Fix the reported issue while preserving visual layout. Return only the corrected fragment."

        original_normalized = _normalize_angular_html(str(node_to_fix).strip())
        for attempt in range(1, 3):
            corrected_fragment_str = _call_llm_for_fix(client, prompt, system_message, screenshot_paths)
            log_openai_call(
                prompt=prompt,
                response=corrected_fragment_str,
                model=OPENAI_MODEL,
                call_type=f"html_post_merge_fix_attempt_{attempt}",
            )
            if not corrected_fragment_str:
                continue

            _, new_node = _parse_llm_html_fragment(corrected_fragment_str)
            if not new_node:
                continue

            new_normalized = _normalize_angular_html(str(new_node).strip())
            if original_normalized.strip() == new_normalized.strip():
                continue

            replaced = _apply_corrected_node_with_fallbacks(
                soup,
                selector,
                html_snippet,
                new_node,
                original_normalized,
            )
            if replaced:
                post_merge_refixed += 1
                break

    if post_merge_refixed:
        print(f"  Post-merge LLM re-fixes applied: {post_merge_refixed}")
    else:
        print("  No additional post-merge LLM re-fixes were needed.")

    safety_net_applied = _apply_non_destructive_a11y_safety_net(soup)
    if safety_net_applied:
        print(f"  Final accessibility safety-net fixes applied: {safety_net_applied}")
    
    if convert_relative_paths:
        soup = convert_paths_to_absolute(soup, base_url)
    print("\n--- Correction process finished ---")
    return str(soup)