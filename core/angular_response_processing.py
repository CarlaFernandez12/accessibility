"""Helpers for parsing and post-processing Angular component LLM responses."""

import re
from typing import Dict, List, Optional


def _parse_component_response(response_text: str) -> Dict[str, Optional[str]]:
    sections = {
        "template": _extract_between_markers(response_text, "<<<TEMPLATE>>>", "<<<END TEMPLATE>>>"),
        "typescript": _extract_between_markers(response_text, "<<<TYPESCRIPT>>>", "<<<END TYPESCRIPT>>>"),
        "styles": _extract_between_markers(response_text, "<<<STYLES>>>", "<<<END STYLES>>>"),
    }

    for key, value in sections.items():
        if value is not None:
            sections[key] = _clean_code_from_markdown(value).strip()

    if sections["template"] is None:
        raise ValueError("Model response does not contain the required <<<TEMPLATE>>> section.")

    return sections


def _clean_code_from_markdown(code: str) -> str:
    """Strip markdown code fences the model may include around returned code."""
    code = re.sub(r'^```[a-z]*\s*\n?', '', code, flags=re.MULTILINE)
    code = re.sub(r'\n?```\s*$', '', code, flags=re.MULTILINE)
    code = re.sub(r'```[a-z]*', '', code)
    code = re.sub(r'```', '', code)
    return code.strip()


def _extract_between_markers(text: str, start_marker: str, end_marker: str) -> Optional[str]:
    start_idx = text.find(start_marker)
    end_idx = text.find(end_marker)
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    return text[start_idx + len(start_marker):end_idx].strip()


def _apply_automatic_contrast_fixes(template_content: str, contrast_errors: List[str]) -> str:
    """Apply automatic contrast fixes to detected elements."""
    lines = template_content.split('\n')
    corrected_lines = []

    for i, line in enumerate(lines, 1):
        corrected_line = line
        for error in contrast_errors:
            if f"Line {i}:" not in error:
                continue

            element_match = re.search(r'Line \d+: Possible contrast error - (\w+)', error)
            if not element_match:
                continue

            element_type = element_match.group(1)
            element_pattern = rf'<{element_type}[^>]*>'
            element_match_in_line = re.search(element_pattern, line, re.IGNORECASE)
            if not element_match_in_line:
                continue

            element_tag = element_match_in_line.group(0)
            if 'style=' not in element_tag:
                corrected_tag = element_tag.rstrip('>') + ' style="color: #000000">'
                corrected_line = line.replace(element_tag, corrected_tag)
                print(f"    → Line {i}: Added style='color: #000000' to <{element_type}>")
            elif 'color:' not in element_tag and 'color=' not in element_tag:
                if 'style="' in element_tag:
                    corrected_tag = element_tag.replace('style="', 'style="color: #000000; ')
                elif "style='" in element_tag:
                    corrected_tag = element_tag.replace("style='", "style='color: #000000; ")
                else:
                    corrected_tag = element_tag.rstrip('>') + ' style="color: #000000">'
                corrected_line = line.replace(element_tag, corrected_tag)
                print(f"    → Line {i}: Added colour: #000000 to existing style of <{element_type}>")

        corrected_lines.append(corrected_line)

    return '\n'.join(corrected_lines)


def _fix_responsive_breaking_changes(original: str, corrected: str) -> str:
    """Revert label changes that would break existing responsive hidden-label patterns."""
    if not original or not corrected:
        return corrected

    original_display_none_labels = re.findall(
        r'<label[^>]*(?:style="[^"]*display\s*:\s*none[^"]*"|class="[^"]*visually-hidden[^"]*")[^>]*>.*?</label>',
        original,
        re.DOTALL | re.IGNORECASE,
    )
    original_hidden_labels = re.findall(
        r'<label[^>]*hidden[^>]*>.*?</label>',
        original,
        re.DOTALL | re.IGNORECASE,
    )
    all_original_labels = original_display_none_labels + original_hidden_labels
    if not all_original_labels:
        return corrected

    for original_label in all_original_labels:
        label_match = re.search(r'<label[^>]*>(.*?)</label>', original_label, re.DOTALL)
        if not label_match:
            continue

        for_attr_match = re.search(r'for="([^"]+)"', original_label)
        if not for_attr_match:
            continue

        for_value = for_attr_match.group(1)
        pattern_block = rf'<label[^>]*for="{re.escape(for_value)}"[^>]*style="[^"]*display\s*:\s*block[^"]*"[^>]*>'
        pattern_no_hidden = rf'<label[^>]*for="{re.escape(for_value)}"[^>]*(?!style="[^"]*display\s*:\s*none)(?!class="[^"]*visually-hidden)(?!hidden)[^>]*>'

        needs_fix = False
        if re.search(pattern_block, corrected, re.IGNORECASE):
            needs_fix = True
        elif re.search(pattern_no_hidden, corrected, re.IGNORECASE):
            corrected_label_match = re.search(
                rf'<label[^>]*for="{re.escape(for_value)}"[^>]*>.*?</label>',
                corrected,
                re.DOTALL | re.IGNORECASE,
            )
            if corrected_label_match:
                corrected_label_full = corrected_label_match.group(0)
                lowered = corrected_label_full.lower()
                if 'display:none' not in lowered and 'visually-hidden' not in lowered and 'hidden' not in lowered:
                    needs_fix = True

        if needs_fix:
            corrected_label_match = re.search(
                rf'<label[^>]*for="{re.escape(for_value)}"[^>]*>.*?</label>',
                corrected,
                re.DOTALL | re.IGNORECASE,
            )
            if corrected_label_match:
                corrected_label_full = corrected_label_match.group(0)
                label_id_match = re.search(r'for="([^"]+)"', corrected_label_full)
                label_content_match = re.search(r'<label[^>]*>(.*?)</label>', corrected_label_full, re.DOTALL)
                if label_id_match and label_content_match:
                    new_label = (
                        f'<label for="{label_id_match.group(1)}" class="visually-hidden">'
                        f'{label_content_match.group(1).strip()}</label>'
                    )
                    corrected = corrected.replace(corrected_label_full, new_label)
                    print("  ⚠️ Detectado cambio que rompe responsive: label con display:block revertido a visually-hidden")

    return corrected


def _apply_automatic_accessibility_fixes(template_content: Optional[str]) -> Optional[str]:
    """Apply a small set of deterministic accessibility fixes the model often misses."""
    if not template_content:
        return template_content

    corrected = template_content

    i_tags = re.finditer(r'<i\s+[^>]*aria-label="[^"]*"[^>]*>', corrected)
    for match in list(i_tags):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)

    nb_icon_tags = re.finditer(r'<nb-icon\s+[^>]*aria-label="[^"]*"[^>]*>', corrected)
    for match in list(nb_icon_tags):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)

    nb_icon_tags_dynamic = re.finditer(r'<nb-icon\s+[^>]*\[attr\.aria-label\]="[^"]*"[^>]*>', corrected)
    for match in list(nb_icon_tags_dynamic):
        tag = match.group(0)
        if 'role=' not in tag:
            corrected = corrected.replace(tag, tag[:-1] + ' role="img">', 1)

    if '<html' in corrected and 'lang=' not in corrected.split('<html')[1].split('>')[0]:
        corrected = re.sub(r'(<html)([^>]*>)', r'\1 lang="en"\2', corrected, count=1)

    progressbar_tags = re.finditer(r'<[^>]*\s+role="progressbar"[^>]*>', corrected)
    for match in list(progressbar_tags):
        tag = match.group(0)
        if 'aria-label=' in tag or 'aria-labelledby=' in tag:
            continue
        valuenow_match = re.search(r'aria-valuenow="([^"]*)"', tag)
        valuenow = valuenow_match.group(1) if valuenow_match else ""
        label_text = f"Progress: {valuenow}%" if valuenow else "Progress indicator"
        corrected = corrected.replace(tag, tag[:-1] + f' aria-label="{label_text}">', 1)

    return corrected


def _fix_basic_syntax_errors(template_content: Optional[str]) -> Optional[str]:
    """Fix common HTML/Angular attribute quoting mistakes introduced by the model."""
    if not template_content:
        return template_content

    def fix_unclosed_attr_in_line(text: str) -> str:
        result = text
        pattern = r'([\(\[\*#]?[\w-]+(?:\([^)]*\))?[\]\)]?)="([^"]*?)([^">])\s*>'

        def replace_attr(match):
            attr_name = match.group(1)
            attr_value = match.group(2)
            last_char = match.group(3)
            if attr_name.startswith('#'):
                return match.group(0)
            return f'{attr_name}="{attr_value}{last_char}">'

        result = re.sub(pattern, replace_attr, result)
        result = re.sub(r'(style="[^"]*?)\s*!important\s*;>', r'\1 !important;">', result)
        result = re.sub(r'(style="[^"]*?[^";])\s*>', r'\1;">', result)
        result = re.sub(r'(data-[\w-]+="[^"]*?)>([A-Za-z])', r'\1">\2', result)
        result = re.sub(r'([\w-]+)="([^"]*?[^\"])\s*>(?!")', r'\1="\2">', result)
        return result

    fixed_lines = []
    for line in template_content.split('\n'):
        fixed_line = fix_unclosed_attr_in_line(line)
        fixed_line = re.sub(r'#(\w+)">', r'#\1>', fixed_line)
        fixed_line = re.sub(r'#(\w+)\s*">', r'#\1>', fixed_line)
        fixed_line = fixed_line.replace('#stepper">', '#stepper>')
        fixed_line = fixed_line.replace('#picker">', '#picker>')
        fixed_line = fixed_line.replace('#drawer">', '#drawer>')
        fixed_lines.append(fixed_line)

    return '\n'.join(fixed_lines)


def _fix_angular_aria_syntax(template_content: Optional[str]) -> Optional[str]:
    """Convert interpolated aria-* attributes to Angular [attr.aria-*] binding syntax."""
    if not template_content:
        return template_content

    pattern_interpolation = r'aria-([a-z-]+)="{{([^}]+)}}"'

    def replace_interpolation(match):
        attr_name = match.group(1)
        expression = match.group(2).strip()
        return f'[attr.aria-{attr_name}]="{expression}"'

    corrected = re.sub(pattern_interpolation, replace_interpolation, template_content)

    pattern_string_interpolation = r'aria-([a-z-]+)="([^"]*)\{\{([^}]+)\}\}([^"]*)"'

    def replace_string_interpolation(match):
        attr_name = match.group(1)
        before = match.group(2)
        expression = match.group(3).strip()
        after = match.group(4)
        parts = []
        if before:
            parts.append(f"'{before}'")
        parts.append(expression)
        if after:
            parts.append(f"'{after}'")
        return f'[attr.aria-{attr_name}]="{" + ".join(parts)}"'

    return re.sub(pattern_string_interpolation, replace_string_interpolation, corrected)