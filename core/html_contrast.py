"""Contrast calculation and contrast-fix context helpers for HTML accessibility flows."""

import re
from typing import Dict, Tuple

# Constants for contrast calculations
CONTRAST_RATIO_MAX = 21.0
CONTRAST_ADJUSTMENT = 0.05
LUMINANCE_THRESHOLD = 0.5

# Candidate colours used when searching for valid contrast combinations
DARK_COLOR_CANDIDATES = [
    '#000000', '#212121', '#424242', '#000080', '#006400',
    '#8B0000', '#4A4A4A', '#2C2C2C'
]
LIGHT_COLOR_CANDIDATES = [
    '#FFFFFF', '#F5F5F5', '#E0E0E0', '#FFD700', '#00FFFF',
    '#FFFF00', '#D3D3D3', '#C0C0C0'
]

# Coefficients for luminance calculation (WCAG)
LUMINANCE_COEFFICIENTS: Dict[str, float] = {
    'r': 0.2126,
    'g': 0.7152,
    'b': 0.0722,
}
LUMINANCE_THRESHOLD_ADJUST = 0.03928
LUMINANCE_ADJUSTMENT_FACTOR = 12.92
LUMINANCE_GAMMA = 2.4


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """Convert a hexadecimal colour into an RGB tuple."""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[index:index + 2], 16) for index in (0, 2, 4))


def _adjust_component_luminance(component: float) -> float:
    """Adjust a single RGB component for luminance computation according to WCAG."""
    if component <= LUMINANCE_THRESHOLD_ADJUST:
        return component / LUMINANCE_ADJUSTMENT_FACTOR
    return ((component + 0.055) / 1.055) ** LUMINANCE_GAMMA


def get_luminance(rgb: Tuple[int, int, int]) -> float:
    """Compute relative luminance according to WCAG 2.1."""
    red, green, blue = [component / 255.0 for component in rgb]
    adjusted_red = _adjust_component_luminance(red)
    adjusted_green = _adjust_component_luminance(green)
    adjusted_blue = _adjust_component_luminance(blue)

    return (
        LUMINANCE_COEFFICIENTS['r'] * adjusted_red
        + LUMINANCE_COEFFICIENTS['g'] * adjusted_green
        + LUMINANCE_COEFFICIENTS['b'] * adjusted_blue
    )


def calculate_contrast_ratio(color1_hex: str, color2_hex: str) -> float:
    """Calculate the WCAG contrast ratio between two colours."""
    lum1 = get_luminance(hex_to_rgb(color1_hex))
    lum2 = get_luminance(hex_to_rgb(color2_hex))
    lighter, darker = max(lum1, lum2), min(lum1, lum2)

    if darker == 0:
        return CONTRAST_RATIO_MAX

    return (lighter + CONTRAST_ADJUSTMENT) / (darker + CONTRAST_ADJUSTMENT)


def find_contrasting_color(bg_color_hex: str, required_ratio: float) -> str:
    """Find a foreground colour that satisfies the required contrast ratio."""
    try:
        bg_luminance = get_luminance(hex_to_rgb(bg_color_hex))
        is_light_bg = bg_luminance > LUMINANCE_THRESHOLD
        candidates = DARK_COLOR_CANDIDATES if is_light_bg else LIGHT_COLOR_CANDIDATES

        for candidate in candidates:
            if calculate_contrast_ratio(candidate, bg_color_hex) >= required_ratio:
                return candidate

        return '#000000' if is_light_bg else '#FFFFFF'
    except Exception:
        return '#000000'


def _calculate_contrast_info(violation):
    """Compute contrast information and generate recommendations."""
    contrast_data = violation.get('contrast_data', {})
    bg_color = contrast_data.get('bgColor', '')
    fg_color = contrast_data.get('fgColor', '')
    current_ratio = contrast_data.get('contrastRatio', 0)
    required_ratio = contrast_data.get('expectedContrastRatio', '4.5:1')
    font_size = contrast_data.get('fontSize', '')
    font_weight = contrast_data.get('fontWeight', 'normal')

    is_large_text = False
    if font_size:
        size_match = re.search(r'(\d+\.?\d*)\s*(?:pt|px)', font_size)
        if size_match and (
            float(size_match.group(1)) >= 18
            or (float(size_match.group(1)) >= 14 and font_weight in ['bold', '700', 'bolder'])
        ):
            is_large_text = True

    contrast_info = ""
    if bg_color and fg_color:
        contrast_info = f"""
**CONTRAST INFORMATION DETECTED**:
- Current background color: {bg_color}
- Current text color: {fg_color}
- Current contrast ratio: {current_ratio}
- Required contrast ratio: {required_ratio}
- Font size: {font_size}
- Font weight: {font_weight}
- Text type: {'Large text (requires 3:1)' if is_large_text else 'Normal text (requires 4.5:1)'}

**IMPORTANT**: Choose a color that guarantees at least a {required_ratio} contrast ratio against the background {bg_color}.
"""

    recommended_color = '#000000'
    color_suggestions = ""
    if bg_color:
        try:
            required_ratio_num = float(required_ratio.replace(':1', ''))
            recommended_color = find_contrasting_color(bg_color, required_ratio_num)
            calculated_ratio = calculate_contrast_ratio(recommended_color, bg_color)
            color_suggestions = f"""
**RECOMMENDED COLOR (GUARANTEED)**:
- Use this exact color: {recommended_color}
- This color has a contrast ratio of {calculated_ratio:.2f}:1 against the background {bg_color}
- It meets the required ratio of {required_ratio}

**IMPORTANT**: Use EXACTLY the color {recommended_color} to guarantee the required contrast ratio.
"""
        except Exception:
            try:
                bg_rgb = tuple(int(bg_color.lower()[index:index + 2], 16) for index in (1, 3, 5))
                bg_luminance = (0.299 * bg_rgb[0] + 0.587 * bg_rgb[1] + 0.114 * bg_rgb[2]) / 255
                recommended_color = '#000000' if bg_luminance > 0.5 else '#FFFFFF'
                color_suggestions = f"**RECOMMENDED COLOR**: {recommended_color} - ensures maximum contrast\n"
            except Exception:
                color_suggestions = "**RECOMMENDED COLOR**: #000000 (black) - safe colour for most light backgrounds\n"

    return contrast_info, color_suggestions, recommended_color, required_ratio


def _get_apply_to_children_text(node_to_fix, text_elements, recommended_color_str):
    """Generate instructions to apply contrast styles to child text nodes too."""
    has_text_children = len(text_elements) > 0
    is_container = node_to_fix.name in ['div', 'section', 'article', 'header', 'footer', 'nav', 'main', 'ul', 'ol']
    if has_text_children or is_container:
        return f"""
**IMPORTANTE - ELEMENTOS HIJOS**:
- El fragmento contiene elementos hijos con texto (como <p>, <span>, <a>, <li>, etc.)
- DEBES aplicar el estilo `color: {recommended_color_str}` al elemento principal Y a TODOS los elementos hijos que contengan texto
- Si el elemento principal es un contenedor, aplica el estilo directamente al contenedor Y a los elementos hijos de texto
- Example: If you have `<div><p>Text</p><span>More text</span></div>`, the result should be:
  `<div style="color: {recommended_color_str}"><p style="color: {recommended_color_str}">Text</p><span style="color: {recommended_color_str}">More text</span></div>`
- NO olvides aplicar el estilo a TODOS los elementos hijos que contengan texto visible
"""
    return ""