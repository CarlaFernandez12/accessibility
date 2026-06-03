"""Shared Angular prompt-building helpers used during component fixing."""

from typing import List, Optional


def _format_detected_errors(detected_errors: List[str]) -> str:
    """Format detected accessibility errors for the LLM prompt."""
    if not detected_errors:
        return ""

    prompts = []
    axe_errors = [error for error in detected_errors if "ERROR AXE:" in error]
    categories = {
        "button_name": [],
        "link_name": [],
        "image_alt": [],
        "label": [],
        "contrast": [],
        "other": [],
    }

    for error in detected_errors:
        error_lower = error.lower()

        if "error axe:" in error_lower:
            continue

        if "button" in error_lower and ("aria-label" in error_lower or "visible text" in error_lower):
            categories["button_name"].append(error)
        elif "link" in error_lower and ("aria-label" in error_lower or "text" in error_lower):
            categories["link_name"].append(error)
        elif "image" in error_lower and "alt" in error_lower:
            categories["image_alt"].append(error)
        elif ("label" in error_lower and "input" in error_lower) or "associated label" in error_lower:
            categories["label"].append(error)
        elif "contrast" in error_lower or "colour" in error_lower or "color" in error_lower:
            categories["contrast"].append(error)
        else:
            categories["other"].append(error)

    def _build_error_specific_prompt(error_type: str, errors: List[str]) -> str:
        error_list = "\n".join(f"- {error}" for error in errors)

        if error_type == "button_name":
            return f"""🔴 BUTTON ACCESSIBILITY ERRORS:\n{error_list}\n\nACTION REQUIRED:\n- Add descriptive visible text or aria-label to all affected buttons\n- If the button only contains an icon, add aria-label describing the action\n- Keep Angular bindings intact"""

        if error_type == "link_name":
            return f"""🔴 LINK ACCESSIBILITY ERRORS:\n{error_list}\n\nACTION REQUIRED:\n- Add descriptive visible text or aria-label to all affected links\n- Replace generic text like 'here', 'more', 'click here' with descriptive labels\n- Keep Angular routerLink/href bindings intact"""

        if error_type == "image_alt":
            return f"""🔴 IMAGE ALT ERRORS:\n{error_list}\n\nACTION REQUIRED:\n- Add meaningful alt text to informative images\n- Use alt=\"\" only for decorative images\n- Keep Angular image bindings intact"""

        if error_type == "label":
            return f"""🔴 FORM LABEL ERRORS:\n{error_list}\n\nACTION REQUIRED:\n- Associate each input/select/textarea with a visible <label> or an aria-label\n- Prefer <label for=...> when possible\n- Keep Angular formControlName/ngModel bindings intact"""

        if error_type == "contrast":
            return f"""🔴 CONTRAST ERRORS:\n{error_list}\n\nACTION REQUIRED:\n- Fix all low-contrast text to meet WCAG AA\n- Prefer changing text colour instead of layout or background\n- Use !important only when necessary to override existing CSS\n- Preserve the current responsive and visual design"""

        return f"""🔴 OTHER ACCESSIBILITY ERRORS:\n{error_list}\n\nACTION REQUIRED:\n- Fix all listed issues while preserving Angular bindings and current layout"""

    if axe_errors:
        error_list = "\n".join(f"- {error}" for error in axe_errors)
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
         - Apply style="color: [correct-colour] !important;" directly to the button
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

    errors_section = _format_detected_errors(detected_errors if detected_errors else [])

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

    return f"""Angular component: {component_name}
Template: {template_path}

TASK: Fix ALL the accessibility errors listed below.

{errors_section}

GENERAL RULES:
- Keep all Angular logic (bindings, *ngIf, *ngFor, pipes, etc.)
- For ARIA attributes with dynamic binding: use [attr.aria-*] instead of aria-*
- For static values: use aria-label=\"fixed text\"
- Do NOT add HTML comments or metadata about fixes

🚨 PRESERVE RESPONSIVE AND VISUAL DESIGN (CRITICAL):
If SCREENSHOTS were provided above, they ARE your visual reference. The final design must look IDENTICAL to the screenshots.

- Do NOT change display:none to display:block - if a label is visually hidden, use aria-label on the input or an sr-only class for the label
- Do NOT add inline styles that break responsive (fixed width, excessive margin/padding, etc.)
- Keep all existing responsive classes (col-sm-*, col-md-*, etc.)
- Do NOT modify layout properties (display, position, flex, grid, width, height, margin, padding) unless critical for accessibility
- If an element has display:none for responsive design, do NOT change it - use aria-label for accessibility instead
- For contrast errors: ONLY adjust text colour (use !important if needed), do NOT change background or layout
- FIX ALL accessibility errors, but do it \"invisibly\" - the visual result must be identical to the screenshots

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