"""Static Angular template analysis helpers for accessibility pre-checks."""

import re
from typing import List, Optional


def _analyze_template_for_accessibility_errors(
    template_content: str,
    style_content: Optional[str] = None,
) -> List[str]:
    """Analyse the template and CSS for obvious accessibility errors using raw text analysis."""
    errors: List[str] = []

    try:
        lines = template_content.split("\n")

        button_pattern = r"<button[^>]*>"
        for line_number, line in enumerate(lines, 1):
            if re.search(button_pattern, line, re.IGNORECASE):
                has_aria_label = (
                    "aria-label=" in line
                    or "[attr.aria-label]" in line
                    or "aria-labelledby=" in line
                )
                button_match = re.search(
                    r"<button[^>]*>(.*?)</button>",
                    line,
                    re.DOTALL | re.IGNORECASE,
                )
                if button_match:
                    button_content = button_match.group(1)
                    button_text = re.sub(
                        r"\{[^}]*\}|<[^>]+>|\*ng[A-Za-z]*=\"[^\"]*\"",
                        "",
                        button_content,
                    ).strip()
                    if not button_text and not has_aria_label:
                        errors.append(
                            f"Line {line_number}: Button without visible text or aria-label"
                        )
                elif not has_aria_label:
                    errors.append(
                        f"Line {line_number}: Button possibly without aria-label (verify manually)"
                    )

        link_pattern = r"<a[^>]*>"
        generic_link_texts = [
            "click aquí",
            "más",
            "aquí",
            "click here",
            "more",
            "here",
            "more info",
            "ver más",
            "read more",
        ]
        for line_number, line in enumerate(lines, 1):
            if re.search(link_pattern, line, re.IGNORECASE):
                has_aria_label = "aria-label=" in line or "[attr.aria-label]" in line
                link_match = re.search(r"<a[^>]*>(.*?)</a>", line, re.DOTALL | re.IGNORECASE)
                if link_match:
                    link_text = re.sub(r"\{[^}]*\}|<[^>]+>", "", link_match.group(1)).strip()
                    if not link_text and not has_aria_label:
                        errors.append(f"Line {line_number}: Link without text or aria-label")
                    elif link_text.lower().strip() in generic_link_texts:
                        errors.append(
                            f"Line {line_number}: Link with generic text '{link_text}' needs descriptive aria-label"
                        )

        input_pattern = r"<(input|select|textarea)[^>]*>"
        input_ids = []
        label_fors = []

        for line_number, line in enumerate(lines, 1):
            input_match = re.search(input_pattern, line, re.IGNORECASE)
            if input_match:
                id_match = re.search(r"\bid=[\"']([^\"']+)[\"']", line)
                if id_match:
                    input_ids.append(id_match.group(1))
                else:
                    has_aria_label = (
                        "aria-label=" in line
                        or "[attr.aria-label]" in line
                        or "aria-labelledby=" in line
                    )
                    if not has_aria_label:
                        errors.append(
                            f"Line {line_number}: Input without id or aria-label (needs associated label)"
                        )

            label_match = re.search(r"<label[^>]*>", line, re.IGNORECASE)
            if label_match:
                for_match = re.search(r"\bfor=[\"']([^\"']+)[\"']", line)
                if for_match:
                    label_fors.append(for_match.group(1))

        for input_id in input_ids:
            if input_id not in label_fors:
                found_aria = False
                for line in lines:
                    if input_id in line and (
                        "aria-label=" in line or "[attr.aria-label]" in line
                    ):
                        found_aria = True
                        break
                if not found_aria:
                    errors.append(
                        f"Input con id='{input_id}' sin label asociado (usar <label for=\"{input_id}\">)"
                    )

        img_pattern = r"<img[^>]*>"
        for line_number, line in enumerate(lines, 1):
            if re.search(img_pattern, line, re.IGNORECASE) and "alt=" not in line:
                errors.append(f"Line {line_number}: Image without alt attribute")

        text_elements_pattern = r"<(p|a|span|div|h[1-6]|label|button)[^>]*>"
        problematic_classes = ["text-muted", "text-secondary", "text-light", "text-gray", "btn"]
        for line_number, line in enumerate(lines, 1):
            if re.search(text_elements_pattern, line, re.IGNORECASE):
                element_match = re.search(
                    r"<(p|a|span|div|h[1-6]|label|button)[^>]*>(.*?)</\1>",
                    line,
                    re.DOTALL | re.IGNORECASE,
                )
                if element_match:
                    element_text = re.sub(r"\{[^}]*\}|<[^>]+>", "", element_match.group(2)).strip()
                    if element_text and len(element_text) > 10:
                        has_explicit_color = (
                            "style=" in line and ("color:" in line or "color=" in line)
                        ) or "[style.color]" in line or "[ngStyle]" in line
                        has_problematic_class = any(css_class in line for css_class in problematic_classes)
                        if not has_explicit_color and (has_problematic_class or "class=" in line):
                            errors.append(
                                f"Line {line_number}: Possible contrast error - {element_match.group(1)} with text without explicit colour (add style='color: #000000')"
                            )

        if style_content:
            errors.extend(_analyze_css_for_contrast_issues(style_content, lines))

    except Exception as exc:
        print(f"  ⚠️ Error analizando template: {exc}")
        import traceback

        traceback.print_exc()

    return errors


def _analyze_css_for_contrast_issues(style_content: str, template_lines: List[str]) -> List[str]:
    """Analyse CSS heuristically to flag likely contrast issues."""
    errors: List[str] = []

    try:
        problematic_classes = [
            "text-muted",
            "text-secondary",
            "text-light",
            "text-white",
            "text-gray-300",
            "text-gray-400",
            "text-gray-500",
        ]
        for line_number, template_line in enumerate(template_lines, 1):
            for problematic_class in problematic_classes:
                if problematic_class in template_line:
                    errors.append(
                        f"Line {line_number}: Possible contrast error - class '{problematic_class}' detected (add style='color: #000000')"
                    )

        for css_line in style_content.split("\n"):
            if not re.search(r"color\s*:", css_line, re.IGNORECASE):
                continue

            color_match = re.search(
                r"color\s*:\s*(#[a-f0-9]{3,6}|rgba?\([^)]+\))",
                css_line,
                re.IGNORECASE,
            )
            if not color_match:
                continue

            color_value = color_match.group(1).lower()
            is_light_color = (
                color_value.startswith("#f")
                or color_value.startswith("#e")
                or color_value.startswith("#d")
                or ("rgba" in color_value and any(alpha in color_value for alpha in ["0.8", "0.7", "0.6", "0.5"]))
            )
            if not is_light_color:
                continue

            selector_match = re.search(r"^[^{]+", css_line)
            if not selector_match:
                continue

            selector = selector_match.group(0).strip()
            selector_token = selector.replace(".", "").replace("#", "")
            for line_number, template_line in enumerate(template_lines, 1):
                if selector_token in template_line:
                    errors.append(
                        f"Line {line_number}: Possible contrast error - light colour '{color_value}' detected in CSS"
                    )
                    break

    except Exception:
        pass

    return errors