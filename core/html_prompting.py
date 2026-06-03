"""Prompt and response helpers for HTML accessibility correction flows."""

from bs4 import BeautifulSoup


def build_contrast_prompt(
    violation,
    original_fragment,
    recommended_color_str,
    apply_to_children,
    contrast_info,
    color_suggestions,
    has_screenshots=False,
):
    """Compact prompt for contrast correction in HTML."""
    description = violation.get('description', 'Color contrast error')
    failure_summary = violation.get('failure_summary', '')

    screenshot_note = ""
    if has_screenshots:
        screenshot_note = "Use the screenshots only as a visual reference; do not change layout or backgrounds."

    parts = [
        "Fix THIS color contrast issue in the following HTML fragment.",
        "",
        f"VIOLATION: {description}",
    ]
    if failure_summary:
        parts.append(f"DETAIL: {failure_summary}")
    if contrast_info:
        parts.append(contrast_info.strip())
    if color_suggestions:
        parts.append(color_suggestions.strip())
    if apply_to_children:
        parts.append(apply_to_children.strip())
    if screenshot_note:
        parts.append(screenshot_note)

    parts.append("")
    parts.append("QUICK RULES:")
    parts.append(f"- Adjust ONLY the text color: style=\"color: {recommended_color_str}\"")
    parts.append("- Keep backgrounds and layout as they are (do not change sizes or positions).")
    parts.append("- If there are child elements with text, apply the new text colour to those elements too.")

    parts.append("")
    parts.append("FRAGMENT TO FIX:")
    parts.append("```html")
    parts.append(original_fragment)
    parts.append("```")
    parts.append("")
    parts.append("Return ONLY the corrected HTML fragment, with no explanations.")

    return "\n".join(parts)


def build_general_prompt(violation, original_fragment, images_info, has_screenshots=False):
    """Compact prompt for general HTML accessibility fixes."""
    description = violation.get('description', 'Accessibility error')
    help_text = violation.get('help', '')
    help_url = violation.get('helpUrl', '')
    failure_summary = violation.get('failure_summary', '')

    screenshot_note = ""
    if has_screenshots:
        screenshot_note = "Keep the visual appearance shown in the screenshots (layout, colours, responsive)."

    lines = [
        "Fix THIS accessibility issue in the following HTML fragment.",
        "",
        f"VIOLATION: {description}",
    ]
    if failure_summary:
        lines.append(f"DETAIL: {failure_summary}")
    if help_text:
        lines.append(f"AXE HELP: {help_text}")
    if help_url:
        lines.append(f"More info: {help_url}")
    if images_info:
        lines.append(images_info.strip())
    if screenshot_note:
        lines.append(screenshot_note)

    lines.append("")
    lines.append("QUICK RULES (by error type):")
    lines.append("- button-name / link-name → add visible text or aria-label=\"...\".")
    lines.append("- image-alt / role-img-alt → add alt=\"...\" or aria-label=\"...\".")
    lines.append("- aria-* → add/fix aria attributes (aria-label, aria-labelledby, role, etc.).")
    lines.append("- focus / keyboard → ensure the element is focusable and keyboard operable.")

    lines.append("")
    lines.append("FRAGMENT TO FIX:")
    lines.append("```html")
    lines.append(original_fragment)
    lines.append("```")
    lines.append("")
    lines.append("Return ONLY the corrected HTML fragment, with no comments or explanations.")

    return "\n".join(lines)


def extract_clean_html(response_content):
    """Extract clean HTML from a raw LLM response."""
    content = response_content.strip()
    if content.startswith("```html"):
        content = content[7:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()


def build_responsive_prompt(original_html, current_html, has_screenshots=False):
    """Build the prompt to restore responsive design."""
    screenshot_instructions = ""
    if has_screenshots:
        screenshot_instructions = """
🚨 CRITICAL - VISUAL REFERENCE:
I have included screenshots that show how the page REALLY looks at different sizes (mobile, tablet, desktop) BEFORE the fixes.

**MANDATORY INSTRUCTIONS**:
1. EXAMINE each screenshot in detail to understand the REAL visual design
2. The final design MUST look IDENTICAL to the screenshots in terms of:
   - Layout and element distribution
   - Sizes and spacing
   - Background colours (do NOT change those visible in the screenshots)
   - Responsive behaviour (how it adapts on mobile/tablet/desktop)
3. KEEP all accessibility fixes (aria-label, alt, roles, contrast styles)
4. The result must be: design from the screenshots + invisible accessibility fixes

"""

    return f"""You are a responsive web design expert. DO A SMART MERGE: combine the responsive design from the original HTML with the accessibility fixes from the current HTML.
{screenshot_instructions}

## CRITICAL GOAL:
Perform an element-by-element MERGE:
- From the ORIGINAL HTML: take ONLY layout CSS properties (width, height, position, display, flex, grid, margin, padding, units)
- From the CURRENT HTML: keep ALL accessibility attributes (aria-label, lang, alt, title, labels, ARIA roles, style="color:...")

## MERGE PROCESS (element by element):
1. For each element in the current HTML, find the corresponding element in the original HTML (by selector/class/id)
2. From the ORIGINAL element: copy ONLY layout CSS properties into the `style` or `class` attribute
3. From the CURRENT element: keep ALL these accessibility attributes:
   - aria-label, aria-labelledby, aria-describedby, aria-current, role
   - lang
   - alt, title (on images)
   - id (if used to associate labels)
   - style="color: ..." (contrast styles) - CRITICAL: NEVER remove these
   - style="background-color: ..." (if added to fix contrast) - CRITICAL: NEVER remove these
   - ANY style attribute containing "color:" or "background-color:" - CRITICAL: NEVER remove these
   - label for="..." (if labels were added)
   - All ARIA attributes that were added
4. Combine both: the final element must have the original's CSS properties + all accessibility attributes from the current one
5. If an element has style="color: ..." or style="background-color: ..." in the CURRENT HTML, you MUST preserve it COMPLETELY in the final result

## STRICT RULES:
1. NEVER remove attributes that start with "aria-"
2. NEVER remove "alt", "title", "lang" attributes
3. NEVER remove `<label>` elements that were added
4. NEVER remove `style="color: ..."` styles that were added - CRITICAL FOR CONTRAST
5. NEVER remove `style="background-color: ..."` styles that were added for contrast - CRITICAL
6. If an element has `style` with "color:" or "background-color:" in the CURRENT HTML, you MUST preserve it COMPLETELY, even when merging with other styles from the original
7. ALWAYS keep the original's classes (they may have responsive CSS)
8. ALWAYS keep the original's layout CSS properties (width, height, position, display, flex, grid, margin, padding)
9. When merging styles, ALWAYS preserve the contrast styles (color, background-color) from the CURRENT HTML first, then add the original's layout styles

## FORBIDDEN:
• Do NOT remove accessibility fixes
• Do NOT restore attributes that would remove the fixes
• Do NOT change the original responsive design (only merge it with the fixes)

## ORIGINAL HTML (reference for responsive design):
```html
{original_html}
```

## CURRENT HTML (with accessibility fixes):
```html
{current_html}
```

⚠️ CRITICAL - IMPORTANT:
1. Both HTML blocks must be COMPLETE in the prompt - do NOT remove any part
2. You must process ALL content from start to end
3. You must include footer, scripts at the end, and any bottom elements
4. The resulting HTML MUST be at least 95% of the original HTML length
5. If the original HTML has 100,000 characters, the result must have at least 95,000 characters
6. Do NOT cut the HTML in half - it must be COMPLETE

**REQUIRED VERIFICATION**: Before responding, verify that your response is approximately the same length as the original HTML. If your response is significantly shorter, you have cut content and must regenerate the full HTML.

Return the COMPLETE HTML doing the MERGE: original's responsive design + ALL accessibility fixes from the current one. The resulting HTML MUST have the same length and full structure as the original."""


def validate_responsive_html(responsive_html, original_html, current_html):
    """Validate and process the resulting responsive HTML."""
    if not responsive_html or "<html" not in responsive_html.lower():
        return None

    original_length = len(original_html)
    responsive_length = len(responsive_html)
    length_ratio = responsive_length / original_length if original_length > 0 else 0

    soup = BeautifulSoup(responsive_html, 'html.parser')
    body = soup.find('body')

    if body and len(body.get_text().strip()) >= 100:
        if length_ratio < 0.7:
            print(f"  ⚠️ WARNING: Resulting HTML is significantly shorter ({length_ratio:.1%} of original)")
        return soup

    print("  ⚠️ WARNING: Body appears empty or truncated")
    return None