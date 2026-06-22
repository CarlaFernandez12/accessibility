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
                model="gpt-5", 
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
            model="gpt-5",
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

def generate_accessible_html_with_parser(original_html, axe_results, media_descriptions, client, base_url, driver, screenshot_paths=None):
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
    if contrast_violations:
        print(f"  Applying deterministic contrast repair to {len(contrast_violations)} violation(s) before the LLM phase.")
        for violation in contrast_violations:
            selector = violation.get('selector', '')
            html_snippet = violation.get('html_snippet', '')
            node_to_fix = _locate_node_to_fix(soup, selector, html_snippet)
            if not node_to_fix:
                continue
            repair_result = apply_manual_contrast_fix(node_to_fix, violation)
            if repair_result:
                manually_repaired_contrast += 1
        print(f"  Deterministic contrast repairs applied: {manually_repaired_contrast}")

    print(f"\n[Phase 2/3] Fixing {len(violations_to_fix)} visible violations...")

    fixed_dot_containers = set()
    successful_fixes = 0
    failed_fixes = 0

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
                continue
            
            violation_id = violation.get('violation_id', 'unknown')
            impact = violation.get('impact', 'moderate')

            print(f"  Fixing '{selector}' for '{violation_id}' (impact: {impact})")
            
            original_fragment = str(node_to_fix)
            images_info = _get_fragment_images(original_fragment, media_descriptions, base_url)
            
            has_screenshots = screenshot_paths is not None and len(screenshot_paths) > 0

            prompt = build_general_prompt(violation, original_fragment, images_info, has_screenshots)
            system_message = "You are a web accessibility expert. Your PRIORITY is to fix ALL mentioned accessibility errors while KEEPING the responsive design shown in the screenshots. Fixes should be visually 'invisible' (use aria-label, roles, alt text). Do NOT add HTML comments or attributes that show they were fixes. The HTML should look like original code, not corrected."
            
            corrected_fragment_str = _call_llm_for_fix(client, prompt, system_message, screenshot_paths)
            log_openai_call(prompt=prompt, response=corrected_fragment_str, model="gpt-5", call_type="html_fix")
            
            if corrected_fragment_str:
                cleaned_response, new_node = _parse_llm_html_fragment(corrected_fragment_str)
                
                if new_node:
                    original_str = str(node_to_fix).strip()
                    new_str = str(new_node).strip()
                    original_normalized = _normalize_angular_html(original_str)
                    new_normalized = _normalize_angular_html(new_str)

                    if original_normalized.strip() == new_normalized.strip():
                        failed_fixes += 1
                        print("    Error: the LLM returned the same code without changes.")
                    else:
                        replaced = _apply_corrected_node_with_fallbacks(
                            soup,
                            selector,
                            html_snippet,
                            new_node,
                            original_normalized,
                        )
                        if replaced:
                            successful_fixes += 1
                        else:
                            failed_fixes += 1
                            print("    Error: could not apply the fix after multiple attempts.")
                            print(f"       Selector: {selector[:100]}")
                else:
                    failed_fixes += 1
                    print("    Error: could not parse the LLM correction.")
                    print(f"       Response preview: {cleaned_response[:200]}...")
            else:
                failed_fixes += 1
                print("    Error: empty response from the LLM.")
            
        except Exception as e:
            failed_fixes += 1
            print(f"  Error while processing '{violation.get('selector', '')}': {e}")
    
    print(f"\n[Summary] Successful fixes: {successful_fixes}, failed fixes: {failed_fixes}")
    
    print(f"\n[Phase 3/3] Restoring responsive design while keeping accessibility fixes...")
    
    try:
        soup = _restore_responsive_design(original_html, soup, client, screenshot_paths)
    except Exception as e:
        print(f"  Warning: responsive merge failed: {e}. Continuing with the current HTML.")
    
    soup = convert_paths_to_absolute(soup, base_url)
    print("\n--- Correction process finished ---")
    return str(soup)