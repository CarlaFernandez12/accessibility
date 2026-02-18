"""
Accessible HTML generation and post‑processing helpers.

This module is responsible for:
    - Guiding colour contrast corrections.
    - Building compact prompts for the LLM.
    - Applying fragment‑level fixes back into the DOM.
    - Performing a final responsive merge while preserving accessibility fixes.

Business logic and behaviour must remain stable; refactors here focus on
clarity, documentation and type hints only.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from lxml import etree

from utils.html_utils import convert_paths_to_absolute
from utils.io_utils import log_openai_call
from utils.violation_utils import flatten_violations, prioritize_violations

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
    'b': 0.0722
}
LUMINANCE_THRESHOLD_ADJUST = 0.03928
LUMINANCE_ADJUSTMENT_FACTOR = 12.92
LUMINANCE_GAMMA = 2.4

def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """
    Convert a hexadecimal colour into an RGB tuple.

    Args:
        hex_color: Colour in hexadecimal format (with or without leading '#').

    Returns:
        Tuple (r, g, b) with integer components in [0, 255].
    """
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def _adjust_component_luminance(component: float) -> float:
    """
    Adjust a single RGB component for luminance computation according to WCAG.
    """
    if component <= LUMINANCE_THRESHOLD_ADJUST:
        return component / LUMINANCE_ADJUSTMENT_FACTOR
    return ((component + 0.055) / 1.055) ** LUMINANCE_GAMMA


def get_luminance(rgb: Tuple[int, int, int]) -> float:
    """
    Compute relative luminance according to WCAG 2.1.

    Args:
        rgb: Tuple (r, g, b) with integer components in [0, 255].

    Returns:
        Relative luminance in [0.0, 1.0].
    """
    r, g, b = [c / 255.0 for c in rgb]
    adjusted_r = _adjust_component_luminance(r)
    adjusted_g = _adjust_component_luminance(g)
    adjusted_b = _adjust_component_luminance(b)
    
    return (
        LUMINANCE_COEFFICIENTS['r'] * adjusted_r +
        LUMINANCE_COEFFICIENTS['g'] * adjusted_g +
        LUMINANCE_COEFFICIENTS['b'] * adjusted_b
    )


def calculate_contrast_ratio(color1_hex: str, color2_hex: str) -> float:
    """
    Calculate the contrast ratio between two colours according to WCAG.

    Args:
        color1_hex: First colour in hexadecimal format.
        color2_hex: Second colour in hexadecimal format.

    Returns:
        Contrast ratio in the range [1.0, 21.0].
    """
    lum1 = get_luminance(hex_to_rgb(color1_hex))
    lum2 = get_luminance(hex_to_rgb(color2_hex))
    lighter, darker = max(lum1, lum2), min(lum1, lum2)
    
    if darker == 0:
        return CONTRAST_RATIO_MAX
    
    return (lighter + CONTRAST_ADJUSTMENT) / (darker + CONTRAST_ADJUSTMENT)


def find_contrasting_color(bg_color_hex: str, required_ratio: float) -> str:
    """
    Find a foreground colour that satisfies the required contrast ratio.

    Args:
        bg_color_hex: Background colour in hexadecimal form.
        required_ratio: Required contrast ratio (e.g. 4.5).

    Returns:
        Hex colour string that meets or exceeds the required ratio
        against the supplied background colour.
    """
    try:
        bg_luminance = get_luminance(hex_to_rgb(bg_color_hex))
        is_light_bg = bg_luminance > LUMINANCE_THRESHOLD
        candidates = (
            DARK_COLOR_CANDIDATES if is_light_bg else LIGHT_COLOR_CANDIDATES
        )
        required = float(required_ratio.replace(':1', ''))
        
        for candidate in candidates:
            if calculate_contrast_ratio(candidate, bg_color_hex) >= required:
                return candidate
        
        return '#000000' if is_light_bg else '#FFFFFF'
    except Exception:
        return '#000000'

def _candidate_image_keys(src_value: Optional[str], base_url: str) -> List[str]:
    """Generate candidate keys for looking up image descriptions."""
    if not src_value:
        return []
    candidates = {src_value}
    try:
        candidates.add(urljoin(base_url, src_value))
        for val in list(candidates):
            parsed = urlparse(val)
            candidates.add(parsed._replace(query='', fragment='').geturl())
    except Exception:
        pass  
    return list(candidates)

def _normalize_angular_selector(selector: str) -> str:
    """
    Normalize a CSS selector by stripping Angular‑specific runtime attributes.

    This is useful when selectors include `_ngcontent-*` or `_nghost-*`
    attributes injected by Angular, which do not exist in the original templates.
    """
    if not selector:
        return selector

    # Remove Angular attributes from the selector: [attr="_ngcontent-xxx"] or [_ngcontent-xxx]
    normalized = re.sub(r'\[_ngcontent-[^\]]+\]', '', selector)
    normalized = re.sub(r'\[_nghost-[^\]]+\]', '', normalized)
    normalized = re.sub(r'\[attr="_ngcontent-[^"]+"\]', '', normalized)
    normalized = re.sub(r'\[attr="_nghost-[^"]+"\]', '', normalized)
    
    # Collapse multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    
    return normalized


def _normalize_angular_html(html_str: Optional[str]) -> Optional[str]:
    """
    Normalize HTML by stripping Angular runtime attributes for comparison.
    """
    if not html_str:
        return html_str

    # Remove _ngcontent-* and _nghost-* attributes
    normalized = re.sub(r'\s+_ngcontent-[^=]*="[^"]*"', '', html_str)
    normalized = re.sub(r'\s+_nghost-[^=]*="[^"]*"', '', normalized)

    return normalized


def _css_to_xpath(css_selector: Optional[str]) -> Optional[str]:
    """
    Convert a CSS selector into a basic XPath expression.

    This is intentionally conservative and only supports a subset of selectors,
    enough for the mapping heuristics used in this module.
    """
    if not css_selector:
        return None

    # Normalize Angular selector first
    css_selector = _normalize_angular_selector(css_selector)
    
    # Strip selector of problematic pseudo-classes first
    xpath = re.sub(r':nth-child\([^)]+\)', '', css_selector)
    xpath = re.sub(r':first-child', '', xpath)
    xpath = re.sub(r':last-child', '', xpath)
    xpath = re.sub(r':nth-of-type\([^)]+\)', '', xpath)
    xpath = re.sub(r':hover', '', xpath)
    xpath = re.sub(r':focus', '', xpath)
    xpath = re.sub(r':active', '', xpath)
    
    # Separar por espacios y > para construir la ruta
    parts = []
    current_part = []
    
    i = 0
    while i < len(xpath):
        char = xpath[i]
        if char == '>':
            if current_part:
                parts.append(''.join(current_part).strip())
                current_part = []
            parts.append('>')
        elif char == ' ':
            if current_part:
                parts.append(''.join(current_part).strip())
                current_part = []
        else:
            current_part.append(char)
        i += 1
    
    if current_part:
        parts.append(''.join(current_part).strip())
    
    # Construir XPath
    xpath_parts = []
    for part in parts:
        if part == '>':
            continue
        if not part:
            continue
            
        # Procesar cada parte
        xpath_part = part
        
        # Reemplazar IDs: #id -> [@id='id']
        xpath_part = re.sub(r'#([a-zA-Z0-9_-]+)', r"[@id='\1']", xpath_part)
        
        # Reemplazar clases: .class -> [contains(@class, 'class')]
        xpath_part = re.sub(r'\.([a-zA-Z0-9_-]+)', r"[contains(@class, '\1')]", xpath_part)
        
        # Si es un tag name, mantenerlo
        if not xpath_part.startswith('[') and not xpath_part.startswith('//'):
            # Es un tag name
            xpath_parts.append(xpath_part)
        else:
            # It's an attribute, add it to the last tag
            if xpath_parts:
                xpath_parts[-1] += xpath_part
            else:
                xpath_parts.append('*' + xpath_part)
    
    # Construir el XPath final
    if not xpath_parts:
        return '//*'
    
    # Determine separator based on whether there was >
    separator = '//' if '>' not in css_selector else '/'
    result = separator + separator.join(xpath_parts)
    
    return result

def _find_node_by_html_snippet(soup, html_snippet):
    """Find a node by comparing its HTML to the violation snippet, ignoring Angular attributes"""
    if not html_snippet or html_snippet == 'No HTML snippet':
        return None
    
    # Normalizar el snippet eliminando atributos Angular
    snippet_clean = _normalize_angular_html(html_snippet)
    snippet_clean = re.sub(r'\s+', ' ', snippet_clean.strip())
    
    # Buscar todos los elementos y comparar su HTML
    all_elements = soup.find_all(True)
    
    for element in all_elements:
        element_html = str(element)
        element_clean = _normalize_angular_html(element_html)
        element_clean = re.sub(r'\s+', ' ', element_clean.strip())
        
        # Comparar atributos clave y contenido (ignorando atributos Angular)
        if snippet_clean in element_clean or element_clean in snippet_clean:
            # Verificar que los atributos principales coincidan
            snippet_soup = BeautifulSoup(html_snippet, 'html.parser')
            snippet_tag = snippet_soup.find()
            
            if snippet_tag:
                # Comparar tag name
                if element.name == snippet_tag.name:
                    # Comparar atributos clave (excluyendo atributos Angular)
                    snippet_attrs = {k for k in snippet_tag.attrs.keys() if not k.startswith('_ng')}
                    element_attrs = {k for k in element.attrs.keys() if not k.startswith('_ng')}
                    
                    # If there are common attributes or the snippet is very similar
                    if snippet_attrs.intersection(element_attrs) or len(snippet_clean) > 50:
                        return element
    
    return None

def _find_node_by_selector(soup, selector, html_snippet=None, violation_index=0):
    """Try to find a node using multiple strategies: CSS, XPath, and HTML snippet matching, with Angular support"""
    # Normalizar selector Angular primero
    normalized_selector = _normalize_angular_selector(selector)
    
    # Estrategia 1: Intentar con CSS selector normalizado (sin atributos Angular)
    try:
        nodes = soup.select(normalized_selector)
        if nodes:
            # If multiple nodes, use violation index or HTML snippet to find the right one
            if len(nodes) == 1:
                return nodes[0]
            elif html_snippet:
                # Normalise HTML snippet for comparison
                snippet_clean = _normalize_angular_html(html_snippet)
                # If multiple nodes, use HTML snippet to find the correct one
                for node in nodes:
                    node_html = str(node)
                    node_clean = _normalize_angular_html(node_html)
                    if snippet_clean in node_clean or node_clean in snippet_clean:
                        return node
                # Si no se encuentra por snippet, devolver el primero
                return nodes[0] if nodes else None
            else:
                # If no snippet, return node at violation index
                return nodes[violation_index % len(nodes)] if nodes else None
    except Exception:
        pass
        
    # Estrategia 1b: Intentar con selector original (por si acaso)
    try:
        nodes = soup.select(selector)
        if nodes:
            if len(nodes) == 1:
                return nodes[0]
            elif html_snippet:
                snippet_clean = _normalize_angular_html(html_snippet)
                for node in nodes:
                    node_html = str(node)
                    node_clean = _normalize_angular_html(node_html)
                    if snippet_clean in node_clean or node_clean in snippet_clean:
                        return node
                return nodes[0] if nodes else None
            else:
                return nodes[violation_index % len(nodes)] if nodes else None
    except Exception:
        pass
    
    # Estrategia 2: Intentar con selector CSS simplificado (sin pseudo-clases y sin atributos Angular)
    try:
        simplified = re.sub(r':nth-child\([^)]+\)|:first-child|:last-child|:nth-of-type\([^)]+\)', '', normalized_selector).strip()
        if simplified:
            nodes = soup.select(simplified)
            if nodes:
                if html_snippet:
                    snippet_clean = _normalize_angular_html(html_snippet)
                    for node in nodes:
                        node_html = str(node)
                        node_clean = _normalize_angular_html(node_html)
                        if snippet_clean in node_clean or node_clean in snippet_clean:
                            return node
                return nodes[0] if nodes else None
    except Exception:
        pass
    
    # Estrategia 3: Intentar con XPath usando lxml
    try:
        # Convertir BeautifulSoup a lxml para usar XPath
        html_str = str(soup)
        parser = etree.HTMLParser()
        tree = etree.fromstring(html_str.encode('utf-8'), parser)
        
        xpath = _css_to_xpath(selector)
        if xpath:
            nodes = tree.xpath(xpath)
            
            if nodes:
                # Convertir nodos lxml de vuelta a BeautifulSoup
                if len(nodes) == 1:
                    # Encontrar el elemento correspondiente en BeautifulSoup usando el HTML
                    node_xml = etree.tostring(nodes[0], encoding='unicode', method='html')
                    if html_snippet and html_snippet in node_xml:
                        node_soup = BeautifulSoup(node_xml, 'html.parser')
                        found = node_soup.find()
                        if found:
                            # Buscar el elemento equivalente en el soup original
                            candidates = soup.find_all(found.name)
                            for candidate in candidates:
                                # Comparar atributos clave
                                if set(found.attrs.keys()) == set(candidate.attrs.keys()):
                                    return candidate
                            return soup.find(found.name, found.attrs) if found else None
                    else:
                        # Sin snippet, usar el primer nodo
                        node_soup = BeautifulSoup(node_xml, 'html.parser')
                        found = node_soup.find()
                        if found:
                            candidates = soup.find_all(found.name)
                            if candidates:
                                return candidates[0]
                elif html_snippet:
                    # Search among multiple nodes using the snippet
                    for node in nodes:
                        node_xml = etree.tostring(node, encoding='unicode', method='html')
                        if html_snippet in node_xml:
                            node_soup = BeautifulSoup(node_xml, 'html.parser')
                            found = node_soup.find()
                            if found:
                                candidates = soup.find_all(found.name)
                                for candidate in candidates:
                                    if set(found.attrs.keys()) == set(candidate.attrs.keys()):
                                        return candidate
                                return soup.find(found.name, found.attrs) if found else None
                else:
                    # No snippet, use node at violation index
                    if violation_index < len(nodes):
                        node_xml = etree.tostring(nodes[violation_index], encoding='unicode', method='html')
                        node_soup = BeautifulSoup(node_xml, 'html.parser')
                        found = node_soup.find()
                        if found:
                            candidates = soup.find_all(found.name)
                            if candidates:
                                return candidates[violation_index % len(candidates)]
    except Exception as e:
        # Silenciar errores de XPath, continuar con otras estrategias
        pass
    
    # Estrategia 4: Usar HTML snippet para encontrar el elemento
    if html_snippet:
        found = _find_node_by_html_snippet(soup, html_snippet)
        if found:
            return found
    
    # Estrategia 5: Extraer clases/IDs del selector y buscar por ellos
    try:
        # Extraer todas las clases del selector (ej: .article-preview, .info, .date)
        class_matches = re.findall(r'\.([a-zA-Z0-9_-]+)', selector)
        if class_matches:
            # Try to find by the last class (the target element)
            target_class = class_matches[-1]
            nodes = soup.find_all(class_=re.compile(f'\\b{re.escape(target_class)}\\b'))
            if nodes and html_snippet:
                # If there's a snippet, find the best match
                snippet_clean = _normalize_angular_html(html_snippet)
                for node in nodes:
                    node_html = str(node)
                    node_clean = _normalize_angular_html(node_html)
                    if snippet_clean[:50] in node_clean or node_clean[:50] in snippet_clean:
                        return node
            if nodes:
                return nodes[0]
        
        # Extraer IDs del selector
        id_matches = re.findall(r'#([a-zA-Z0-9_-]+)', selector)
        if id_matches:
            target_id = id_matches[-1]
            node = soup.find(id=target_id)
            if node:
                return node
        
        # Extraer atributos del selector (ej: [aria-label="..."], [href$="..."])
        attr_matches = re.findall(r'\[([^\]]+)\]', selector)
        if attr_matches:
            for attr_match in reversed(attr_matches):  # Start from the last
                if '=' in attr_match:
                    attr_name, attr_value = attr_match.split('=', 1)
                    attr_value = attr_value.strip('"\'')
                    # Buscar por atributo
                    nodes = soup.find_all(attrs={attr_name: attr_value})
                    if nodes:
                        if html_snippet:
                            snippet_clean = _normalize_angular_html(html_snippet)
                            for node in nodes:
                                node_html = str(node)
                                node_clean = _normalize_angular_html(node_html)
                                if snippet_clean[:50] in node_clean or node_clean[:50] in snippet_clean:
                                    return node
                        return nodes[0]
    except Exception:
        pass
    
    # Strategy 6: Last resort - simplified selector (last part)
    try:
        last_part = selector.split('>')[-1].strip()
        last_part = re.sub(r':[a-z-]+(\([^)]+\))?', '', last_part)
        if last_part:
            nodes = soup.select(last_part)
            if nodes:
                if html_snippet:
                    # Try to find the one that best matches the snippet
                    snippet_clean = _normalize_angular_html(html_snippet)
                    for node in nodes:
                        node_html = str(node)
                        node_clean = _normalize_angular_html(node_html)
                        if snippet_clean[:50] in node_clean or node_clean[:50] in snippet_clean:
                            return node
                return nodes[0]
    except Exception:
        pass
    
    # Strategy 7: Search by tag name of the selector's last element
    try:
        # Extract tag name of last element
        parts = selector.split('>')
        last_part = parts[-1].strip()
        # Quitar pseudo-clases, clases, IDs
        tag_name = re.sub(r'[.:#\[].*', '', last_part).strip()
        if tag_name and tag_name[0].isalpha():
            nodes = soup.find_all(tag_name)
            if nodes and html_snippet:
                snippet_clean = _normalize_angular_html(html_snippet)
                for node in nodes:
                    node_html = str(node)
                    node_clean = _normalize_angular_html(node_html)
                    # Look for stricter matches
                    if snippet_clean in node_clean or node_clean in snippet_clean:
                        return node
                    # Or at least search by text content if small
                    if len(snippet_clean) < 200 and node.get_text(strip=True) in snippet_clean:
                        return node
    except Exception:
        pass
    
    return None

def _fix_owl_controls(node_to_fix, violation, fixed_dot_containers):
    """Fix Owl Carousel controls with heuristics"""
    violation_id_val = (violation.get('violation_id') or violation.get('id') or '').lower()
    description_val = (violation.get('description') or '').lower()
    # No abortar si no coincide exactamente: intentaremos etiquetar botones sin texto igualmente
    
    class_list = node_to_fix.get('class', [])
    if isinstance(class_list, str):
        class_list = class_list.split()
    role_val = node_to_fix.get('role', '')

    if 'owl-prev' in class_list or ('prev' in class_list and node_to_fix.name == 'button'):
        node_to_fix['aria-label'] = 'Previous slide'
        return True
    elif 'owl-next' in class_list or ('next' in class_list and node_to_fix.name == 'button'):
        node_to_fix['aria-label'] = 'Next slide'
        return True
    elif 'owl-dot' in class_list:
        dots_container = node_to_fix.find_parent(class_='owl-dots')
        if dots_container and id(dots_container) not in fixed_dot_containers:
            for idx, dot in enumerate(dots_container.find_all('button', class_='owl-dot')):
                dot['aria-label'] = f'Go to slide {idx + 1}'
            fixed_dot_containers.add(id(dots_container))
        return True
    elif (node_to_fix.name == 'button' or role_val == 'button') and not node_to_fix.get('aria-label'):
        label_candidates = {
            'plus': 'Agregar', 'bi-plus': 'Agregar', 'bi-plus-lg': 'Agregar',
            'add': 'Agregar', 'close': 'Cerrar', 'x': 'Cerrar', 'bi-x': 'Cerrar',
            'search': 'Buscar', 'bi-search': 'Buscar', 'delete': 'Eliminar',
            'trash': 'Eliminar', 'bi-trash': 'Eliminar',
        }
        label = node_to_fix.get('title') or node_to_fix.get_text().strip()
        if not label and isinstance(class_list, list):
            joined = ' '.join(class_list).lower()
            for key, val in label_candidates.items():
                if key in joined:
                    label = val
                    break
        if not label:
            label = 'Button'
        node_to_fix['aria-label'] = label
        return True

    return False

def _fix_link_name(node_to_fix, violation):
    """Fix links without discernible text by adding aria-label or visible text"""
    if node_to_fix.name != 'a':
        return False
    
    # Verificar si ya tiene texto discernible
    has_text = node_to_fix.get_text(strip=True) != ''
    has_aria_label = (node_to_fix.get('aria-label') or '').strip() != ''
    has_title = (node_to_fix.get('title') or '').strip() != ''
    
    if has_text or has_aria_label:
        return False  # Ya tiene texto discernible
    
    # Intentar obtener un label del href, title, o clases
    href = node_to_fix.get('href', '')
    title = node_to_fix.get('title', '')
    class_list = node_to_fix.get('class', [])
    if isinstance(class_list, str):
        class_list = class_list.split()
    
    # Intentar inferir el label desde diferentes fuentes
    label = None
    
    # 1. Usar title si existe
    if title:
        label = title
    # 2. Intentar inferir desde el href
    elif href:
        # Extraer texto descriptivo del href
        if href.startswith('http'):
            from urllib.parse import urlparse
            parsed = urlparse(href)
            domain = parsed.netloc.replace('www.', '')
            if domain:
                label = f'Enlace a {domain}'
        elif href.startswith('#'):
            label = 'Enlace interno'
        elif href.startswith('mailto:'):
            label = f'Enviar correo a {href.replace("mailto:", "")}'
        elif href.startswith('tel:'):
            label = f'Llamar a {href.replace("tel:", "")}'
        else:
            # Usar el nombre del archivo o ruta
            path_parts = href.split('/')
            if path_parts:
                last_part = path_parts[-1].replace('.html', '').replace('.htm', '').replace('-', ' ').replace('_', ' ')
                if last_part:
                    label = f'Enlace a {last_part.title()}'
    # 3. Intentar inferir desde clases CSS
    if not label and class_list:
        label_candidates = {
            'home': 'Home', 'menu': 'Menu', 'nav': 'Navigation',
            'logo': 'Logo', 'icon': 'Icono', 'social': 'Red social',
            'facebook': 'Facebook', 'twitter': 'Twitter', 'instagram': 'Instagram',
            'linkedin': 'LinkedIn', 'youtube': 'YouTube', 'email': 'Correo',
            'phone': 'Phone', 'contact': 'Contact', 'about': 'About',
            'next': 'Next', 'prev': 'Previous', 'back': 'Back',
            'more': 'More information', 'read': 'Read more', 'download': 'Download'
        }
        joined = ' '.join(class_list).lower()
        for key, val in label_candidates.items():
            if key in joined:
                label = val
                break
    
    # 4. Si hay un icono dentro, intentar inferir desde el icono
    if not label:
        icon = node_to_fix.find(['i', 'svg', 'img'])
        if icon:
            icon_class = icon.get('class', [])
            if isinstance(icon_class, str):
                icon_class = icon_class.split()
            icon_classes_str = ' '.join(icon_class).lower()
            if 'fa-home' in icon_classes_str or 'home' in icon_classes_str:
                label = 'Inicio'
            elif 'fa-envelope' in icon_classes_str or 'email' in icon_classes_str:
                label = 'Correo'
            elif 'fa-phone' in icon_classes_str or 'phone' in icon_classes_str:
                label = 'Phone'
            elif 'fa-facebook' in icon_classes_str:
                label = 'Facebook'
            elif 'fa-twitter' in icon_classes_str:
                label = 'Twitter'
            elif 'fa-instagram' in icon_classes_str:
                label = 'Instagram'
            elif 'fa-linkedin' in icon_classes_str:
                label = 'LinkedIn'
            elif 'fa-youtube' in icon_classes_str:
                label = 'YouTube'
            elif 'fa-arrow-right' in icon_classes_str or 'next' in icon_classes_str:
                label = 'Siguiente'
            elif 'fa-arrow-left' in icon_classes_str or 'prev' in icon_classes_str:
                label = 'Anterior'
    
    # 5. If still no label, use a generic one based on context
    if not label:
        # Verificar si hay un elemento hermano con texto que pueda servir de contexto
        parent = node_to_fix.find_parent()
        if parent:
            siblings = parent.find_all('a', limit=5)
            if len(siblings) > 1:
                idx = siblings.index(node_to_fix) if node_to_fix in siblings else 0
                label = f'Enlace {idx + 1}'
            else:
                label = 'Enlace'
        else:
            label = 'Enlace'
    
    # Aplicar el label
    if label:
        node_to_fix['aria-label'] = label
        return True

    return False

def _build_contrast_prompt(violation, original_fragment, recommended_color_str, apply_to_children, contrast_info, color_suggestions, has_screenshots=False):
    """Compact prompt for contrast correction in HTML."""
    description = violation.get('description', 'Error de contraste de color')
    failure_summary = violation.get('failure_summary', '')

    screenshot_note = ""
    if has_screenshots:
        screenshot_note = "Usa las capturas solo como referencia visual; no cambies el layout ni los fondos."

    parts = [
        f"Corrige ESTE error de contraste de color en el siguiente fragmento HTML.",
        "",
        f"VIOLATION: {description}",
    ]
    if failure_summary:
        parts.append(f"DETALLE: {failure_summary}")
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
    parts.append(f"- Ajusta SOLO el color del texto: style=\"color: {recommended_color_str}\"")
    parts.append("- Keep backgrounds and layout as they are (do not change sizes or positions).")
    parts.append("- If there are child elements with text, apply the new text colour to those elements too.")

    parts.append("")
    parts.append("FRAGMENTO A CORREGIR:")
    parts.append("```html")
    parts.append(original_fragment)
    parts.append("```")
    parts.append("")
    parts.append("Devuelve SOLO el fragmento HTML corregido, sin explicaciones.")

    return "\n".join(parts)

def _build_general_prompt(violation, original_fragment, images_info, has_screenshots=False):
    """Prompt compacto para correcciones generales de accesibilidad en HTML."""
    description = violation.get('description', 'Error de accesibilidad')
    help_text = violation.get('help', '')
    help_url = violation.get('helpUrl', '')
    failure_summary = violation.get('failure_summary', '')
    
    screenshot_note = ""
    if has_screenshots:
        screenshot_note = "Keep the visual appearance shown in the screenshots (layout, colours, responsive)."

    lines = [
        "Corrige ESTE error de accesibilidad en el siguiente fragmento HTML.",
        "",
        f"VIOLATION: {description}",
    ]
    if failure_summary:
        lines.append(f"DETALLE: {failure_summary}")
    if help_text:
        lines.append(f"AYUDA (Axe): {help_text}")
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
    lines.append("FRAGMENTO A CORREGIR:")
    lines.append("```html")
    lines.append(original_fragment)
    lines.append("```")
    lines.append("")
    lines.append("Devuelve SOLO el fragmento HTML corregido, sin comentarios ni explicaciones.")

    return "\n".join(lines)
            
def _extract_clean_html(response_content):
    """Extrae HTML limpio de la respuesta del LLM"""
    content = response_content.strip()
    if content.startswith("```html"):
        content = content[7:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()

def _process_image_descriptions(soup, media_descriptions, base_url):
    """Apply image descriptions to img tags"""
    for img_tag in soup.find_all('img'):
        src = img_tag.get('src')
        for key in _candidate_image_keys(src, base_url):
            if key in media_descriptions:
                img_tag['alt'] = img_tag['title'] = media_descriptions[key]
                break

def _get_text_elements(node):
    """Obtiene elementos hijos que contienen texto"""
    text_tags = ['p', 'span', 'a', 'li', 'td', 'th', 'label', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'b', 'i']
    text_elements = []
    for tag in text_tags:
        for child in node.find_all(tag, recursive=True):
            if child.get_text(strip=True):
                text_elements.append(child)
    return text_elements

def _get_fragment_images(fragment_html, media_descriptions, base_url):
    """Extract image information from the fragment"""
    fragment_soup = BeautifulSoup(fragment_html, 'html.parser')
    fragment_images = []
    for img in fragment_soup.find_all('img'):
        img_src = img.get('src', '')
        if img_src:
            for key in _candidate_image_keys(img_src, base_url):
                if key in media_descriptions:
                    fragment_images.append(f"  - {img_src}: {media_descriptions[key]}")
                    break
    if fragment_images:
        return f"\n**Available image descriptions**:\n" + "\n".join(fragment_images) + "\nIMPORTANT: If the fragment contains images, use these descriptions for the `alt` and `title` attributes. KEEP these descriptions exact.\n"
    return ""

    



def _calculate_contrast_info(violation):
    """Compute contrast information and generate recommendations"""
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
        if size_match and (float(size_match.group(1)) >= 18 or (float(size_match.group(1)) >= 14 and font_weight in ['bold', '700', 'bolder'])):
            is_large_text = True
    
    contrast_info = ""
    if bg_color and fg_color:
        contrast_info = f"""
**CONTRAST INFORMATION DETECTED**:
- Color de fondo actual: {bg_color}
- Color de texto actual: {fg_color}
- Ratio de contraste actual: {current_ratio}
- Ratio de contraste requerido: {required_ratio}
- Font size: {font_size}
- Peso de fuente: {font_weight}
- Tipo de texto: {'Texto grande (requiere 3:1)' if is_large_text else 'Texto normal (requiere 4.5:1)'}

**IMPORTANTE**: Debes elegir un color que garantice un contraste de al menos {required_ratio} con el fondo {bg_color}.
"""
    
    recommended_color = '#000000'
    color_suggestions = ""
    if bg_color:
        try:
            required_ratio_num = float(required_ratio.replace(':1', ''))
            recommended_color = find_contrasting_color(bg_color, required_ratio_num)
            calculated_ratio = calculate_contrast_ratio(recommended_color, bg_color)
            color_suggestions = f"""
**COLOR RECOMENDADO (GARANTIZADO)**:
- Usa este color exacto: {recommended_color}
- Este color tiene un contraste de {calculated_ratio:.2f}:1 con el fondo {bg_color}
- Cumple con el ratio requerido de {required_ratio}

**IMPORTANTE**: Debes usar EXACTAMENTE el color {recommended_color} para garantizar que se cumpla el ratio de contraste.
"""
        except:
            try:
                bg_rgb = tuple(int(bg_color.lower()[i:i+2], 16) for i in (1, 3, 5))
                bg_luminance = (0.299 * bg_rgb[0] + 0.587 * bg_rgb[1] + 0.114 * bg_rgb[2]) / 255
                recommended_color = '#000000' if bg_luminance > 0.5 else '#FFFFFF'
                color_suggestions = f"**RECOMMENDED COLOR**: {recommended_color} - ensures maximum contrast\n"
            except:
                color_suggestions = "**RECOMMENDED COLOR**: #000000 (black) - safe colour for most light backgrounds\n"
    
    return contrast_info, color_suggestions, recommended_color, required_ratio

def _get_apply_to_children_text(node_to_fix, text_elements, recommended_color_str):
    """Genera texto para aplicar estilos a elementos hijos"""
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

def _call_llm_for_fix(client, prompt, system_message, screenshot_paths=None):
    """Llama al LLM para corregir un fragmento"""
    messages = [
        {"role": "system", "content": system_message}, 
    ]
    
    # Si hay capturas, incluirlas en el mensaje del usuario
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
                print(f"  ⚠️ Error al incluir captura {screenshot_path}: {e}")
        messages.append({"role": "user", "content": user_content})
    else:
        messages.append({"role": "user", "content": prompt})
    
    response = client.chat.completions.create(
                model="gpt-4o", 
                messages=messages, 
                temperature=0.0
            )
    return _extract_clean_html(response.choices[0].message.content)

def _build_responsive_prompt(original_html, current_html, has_screenshots=False):
    """Build the prompt to restore responsive design"""
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
        
def _validate_responsive_html(responsive_html, original_html, current_html):
    """Validate and process the resulting responsive HTML"""
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
    else:
        print("  ⚠️ WARNING: Body appears empty or truncated")
        return None

def _ensure_discernible_buttons(soup):
    """Ensure icon-only buttons have discernible text via aria-label."""
    print("--- [DEBUG] Iniciando _ensure_discernible_buttons (v2) ---")
    label_candidates = {
        'bi-plus-lg': 'Agregar', 'bi-plus': 'Agregar', 'plus': 'Agregar', 'add': 'Agregar',
        'bi-x': 'Cerrar', 'x': 'Cerrar', 'close': 'Cerrar',
        'bi-search': 'Buscar', 'search': 'Buscar',
        'bi-trash': 'Eliminar', 'trash': 'Eliminar', 'delete': 'Eliminar',
    }
    
    buttons = set(soup.find_all('button'))
    buttons.update(soup.find_all(role='button'))
    print(f"  > [DEBUG] Found {len(buttons)} button-type elements.")

    for btn in buttons:
        classes_str = ' '.join(btn.get('class', []))
        
        # 1. Comprobar si tiene texto visible
        has_text = (btn.get_text() or '').strip() != ''
        
        # 2. Check if it has an existing non-empty aria-label
        has_aria_label = (btn.get('aria-label') or '').strip() != ''
        
        # If it has text OR a valid aria-label, it's fine. Move to next.
        if has_text or has_aria_label:
            if has_text:
                print(f"\n  > [DEBUG] SALTANDO (tiene texto): <{btn.name} class='{classes_str}'>")
            if has_aria_label:
                print(f"\n  > [DEBUG] SALTANDO (ya tiene aria-label): <{btn.name} class='{classes_str}' aria-label='{btn.get('aria-label')}'>")
            continue

        # If we're here, it's an icon-only button with NO text and NO aria-label.
        # NECESITA ser corregido, independientemente de lo que tenga en 'title'.
        print(f"\n  > [DEBUG] PROCESANDO: <{btn.name} class='{classes_str}'>")
        print(f"    ... tiene texto?   {has_text}")
        print(f"    ... tiene aria-label? {has_aria_label}")
        print(f"    ... tiene title?      {btn.get('title')}") # Solo para info

        # 3. Intentar inferir una etiqueta desde las clases CSS
        joined_classes = ' '.join(btn.get('class', [])).lower()
        inferred_label = None
        for key, val in label_candidates.items():
            if key in joined_classes:
                inferred_label = val
                break
        
        final_label = None
        if inferred_label:
            final_label = inferred_label
            print(f"    > Inferencia por clase: '{key}' -> '{final_label}'")
        else:
            # 4. If not inferred, use 'title' if it exists and is non-empty
            title_val = (btn.get('title') or '').strip()
            if title_val:
                final_label = title_val
                print(f"    > Usando 'title' existente: '{final_label}'")
            else:
                # 5. As a last resort, use a generic label
                final_label = 'Button'
                print(f"    > Usando etiqueta por defecto: '{final_label}'")

        print(f"    > RESULTADO: APLICANDO ETIQUETA '{final_label}'")
        btn['aria-label'] = final_label
    
    print("--- [DEBUG] Finalizado _ensure_discernible_buttons (v2) ---")


def _ensure_discernible_links(soup):
    """Asegura que los enlaces sin texto tengan nombre accesible (link-name)."""
    print("--- [DEBUG] Iniciando _ensure_discernible_links ---")
    fixed_count = 0
    for a_tag in soup.find_all('a'):
        try:
            # Reuse _fix_link_name heuristic logic
            fixed = _fix_link_name(a_tag, {})
            if fixed:
                fixed_count += 1
        except Exception as e:
            print(f"  ⚠️ Error corrigiendo enlace sin texto discernible: {e}")
    print(f"  > [DEBUG] Enlaces corregidos (link-name): {fixed_count}")
    print("--- [DEBUG] Finalizado _ensure_discernible_links ---")

def generate_accessible_html_with_parser(original_html, axe_results, media_descriptions, client, base_url, driver, screenshot_paths=None):
    print("\n--- Starting LLM-only correction process ---")
    
    soup = BeautifulSoup(original_html, 'html.parser')
    all_violations = flatten_violations(axe_results.get('violations', []))
    
    if not all_violations:
        print("No se encontraron violaciones procesables.")
        return original_html

    print(f"\n[Fase 1/3] Procesando violaciones...")
    violations_to_fix = [v for v in all_violations if v.get('selector') and v.get('selector') != 'No selector']
    violations_to_fix = prioritize_violations(violations_to_fix)
    
    _process_image_descriptions(soup, media_descriptions, base_url)

    print(f"\n[Fase 2/3] Corrigiendo {len(violations_to_fix)} violaciones en elementos visibles...")
    
    fixed_dot_containers = set()
    successful_fixes = 0
    failed_fixes = 0

    print(f"  → Procesando {len(violations_to_fix)} violaciones en total")
    
    # Mostrar resumen de tipos de violaciones
    violation_types = {}
    for v in violations_to_fix:
        v_id = v.get('violation_id', 'unknown')
        violation_types[v_id] = violation_types.get(v_id, 0) + 1
    
    if violation_types:
        print(f"  → Tipos de violaciones encontradas:")
        for v_type, count in sorted(violation_types.items(), key=lambda x: x[1], reverse=True):
            print(f"     - {v_type}: {count} violation(s)")
    
    for violation in violations_to_fix:
        try:
            selector = violation.get('selector', '')
            html_snippet = violation.get('html_snippet', '')
            violation_id = violation.get('violation_id', 'unknown')

            # Normalizar selector Angular primero
            normalized_selector = _normalize_angular_selector(selector)

            # Try to find the element - original simple method that worked best
            node_to_fix = None
            try:
                # Method 1: Try with normalised selector (no Angular attributes)
                node_to_fix = soup.select_one(normalized_selector)
            except Exception:
                pass

            # Method 2: Try with original selector (just in case)
            if not node_to_fix:
                try:
                    node_to_fix = soup.select_one(selector)
                except Exception:
                    pass

            # If not found, try with select (original alternative method)
            if not node_to_fix:
                try:
                    nodes = soup.select(normalized_selector)
                    if not nodes:
                        nodes = soup.select(selector)
                    if nodes:
                        # If multiple, use normalised HTML snippet to find the right one
                        if len(nodes) == 1:
                            node_to_fix = nodes[0]
                        elif html_snippet:
                            snippet_clean = _normalize_angular_html(html_snippet)
                            for node in nodes:
                                node_html = str(node)
                                node_clean = _normalize_angular_html(node_html)
                                if snippet_clean[:100] in node_clean or node_clean[:100] in snippet_clean:
                                    node_to_fix = node
                                    break
                            if not node_to_fix:
                                # Si no se encuentra por snippet, usar el primero
                                node_to_fix = nodes[0]
                        else:
                            # Sin snippet, usar el primero
                            node_to_fix = nodes[0]
                except Exception:
                    pass

            # If still not found, try simplified selector (original method)
            if not node_to_fix:
                try:
                    simplified = re.sub(r':nth-child\([^)]+\)|:first-child|:last-child|:nth-of-type\([^)]+\)', '', normalized_selector).strip()
                    if simplified:
                        node_to_fix = soup.select_one(simplified)
                except Exception:
                    pass

            # If still not found, use improved XPath function (includes Angular normalisation)
            if not node_to_fix:
                node_to_fix = _find_node_by_selector(soup, selector, html_snippet, 0)

            # Last attempt: search by HTML snippet directly
            if not node_to_fix:
                print(f"  ⚠️ No element found for selector: {selector[:50]}... (trying HTML snippet and advanced strategies)")
                if html_snippet:
                    node_to_fix = _find_node_by_html_snippet(soup, html_snippet)

                if not node_to_fix:
                    # Advanced strategies (5-7) were already tried in _find_node_by_selector
                    # including search by classes, IDs and attributes extracted from the selector
                    print(f"  ✗ No se pudo encontrar elemento para: {selector[:50]}...")
                    print(f"     Selector completo: {selector[:150]}")
                    if html_snippet:
                        print(f"     HTML snippet: {html_snippet[:100]}...")
                    failed_fixes += 1
                continue
            
            violation_id = violation.get('violation_id', 'unknown')
            violation_id_lower = violation_id.lower()
            description_val = (violation.get('description') or '').lower()
            impact = violation.get('impact', 'moderate')

            # Usar LLM directamente para todas las correcciones (como antes)
            print(f"  > FIX (IA): Procesando '{selector}' para '{violation_id}' (impacto: {impact})")
            
            original_fragment = str(node_to_fix)
            images_info = _get_fragment_images(original_fragment, media_descriptions, base_url)
            
            has_screenshots = screenshot_paths is not None and len(screenshot_paths) > 0
            
            if 'color-contrast' in violation_id_lower or ('color' in violation_id_lower and 'contrast' in violation_id_lower):
                text_elements = _get_text_elements(node_to_fix) if 'color-contrast' in violation_id_lower else []
                contrast_info, color_suggestions, recommended_color, required_ratio = _calculate_contrast_info(violation)
                apply_to_children = _get_apply_to_children_text(node_to_fix, text_elements, recommended_color)
                prompt = _build_contrast_prompt(violation, original_fragment, recommended_color, apply_to_children, contrast_info, color_suggestions, has_screenshots)
                system_message = f"You are an accessibility expert. FIX the colour contrast by adding the style attribute with color: {recommended_color}. You MUST fix this error. If there are child elements with text, apply the style to them too. Do NOT add other unnecessary attributes. KEEP the responsive design as shown in the screenshots (if available)."
            else:
                prompt = _build_general_prompt(violation, original_fragment, images_info, has_screenshots)
                system_message = "You are a web accessibility expert. Your PRIORITY is to fix ALL mentioned accessibility errors while KEEPING the responsive design shown in the screenshots. Fixes should be visually 'invisible' (use aria-label, roles, alt text). Do NOT add HTML comments or attributes that show they were fixes. The HTML should look like original code, not corrected."
            
            corrected_fragment_str = _call_llm_for_fix(client, prompt, system_message, screenshot_paths)
            log_openai_call(prompt=prompt, response=corrected_fragment_str, model="gpt-4o", call_type="html_fix")
            
            if corrected_fragment_str:
                # Strip possible markdown code around the response
                cleaned_response = corrected_fragment_str.strip()
                if cleaned_response.startswith("```"):
                    parts = cleaned_response.split("```")
                    if len(parts) >= 3:
                        code_block = parts[1]
                        if "\n" in code_block:
                            # Quitar etiqueta de lenguaje si existe
                            code_block = code_block.split("\n", 1)[1]
                        cleaned_response = code_block.strip()
                    else:
                        cleaned_response = cleaned_response.replace("```html", "").replace("```", "").strip()
                
                # Intentar parsear el HTML corregido
                new_node = None
                try:
                    parsed_soup = BeautifulSoup(cleaned_response, 'html.parser')
                    new_node = parsed_soup.find()
                except Exception as parse_error:
                    print(f"    ⚠️ Error parseando respuesta del LLM: {parse_error}")
                    # Intentar extraer solo el HTML del tag principal
                    try:
                        # Find the first valid HTML tag
                        import re
                        tag_match = re.search(r'<[a-zA-Z][^>]*>.*?</[a-zA-Z]+>', cleaned_response, re.DOTALL)
                        if tag_match:
                            cleaned_response = tag_match.group(0)
                            parsed_soup = BeautifulSoup(cleaned_response, 'html.parser')
                            new_node = parsed_soup.find()
                    except Exception:
                        pass
                
                if new_node:
                    # Validar que realmente hubo cambios significativos
                    original_str = str(node_to_fix).strip()
                    new_str = str(new_node).strip()
                    # Normalizar ambos para comparar
                    original_normalized = _normalize_angular_html(original_str)
                    new_normalized = _normalize_angular_html(new_str)
                    
                    # If identical after normalising, the LLM made no changes
                    if original_normalized.strip() == new_normalized.strip():
                        failed_fixes += 1
                        print(f"    ✗ Error: LLM returned the same code with no fixes")
                    else:
                        # Intentar reemplazar el nodo
                        replaced = False
                        try:
                            node_to_fix.replace_with(new_node)
                            replaced = True
                            successful_fixes += 1
                            print(f"    ✓ Corregido exitosamente")
                        except Exception as replace_error:
                            # Si el reemplazo falla, intentar encontrar el elemento usando estrategias avanzadas
                            print(f"    ⚠️ Error en reemplazo inicial: {replace_error}, intentando estrategias alternativas...")
                            
                            # Estrategia 1: Buscar el nodo nuevamente usando el selector
                            try:
                                nodes = soup.select(selector)
                                if not nodes:
                                    # Intentar con selector normalizado
                                    normalized_sel = _normalize_angular_selector(selector)
                                    nodes = soup.select(normalized_sel)
                                if nodes:
                                    # Find the node that best matches the original
                                    for candidate_node in nodes:
                                        try:
                                            candidate_normalized = _normalize_angular_html(str(candidate_node))
                                            if original_normalized[:100] in candidate_normalized or candidate_normalized[:100] in original_normalized:
                                                candidate_node.replace_with(new_node)
                                                replaced = True
                                                successful_fixes += 1
                                                print(f"    ✓ Fixed successfully (after retry)")
                                                break
                                        except Exception:
                                            continue
                                    # If no match found but there are nodes, use the first
                                    if not replaced and nodes:
                                        nodes[0].replace_with(new_node)
                                        replaced = True
                                        successful_fixes += 1
                                        print(f"    ✓ Corregido exitosamente (usando primer nodo encontrado)")
                            except Exception as retry_error:
                                pass
                            
                            # Estrategia 2: Buscar por HTML snippet si tenemos uno
                            if not replaced and html_snippet:
                                try:
                                    found_node = _find_node_by_html_snippet(soup, html_snippet)
                                    if found_node:
                                        found_node.replace_with(new_node)
                                        replaced = True
                                        successful_fixes += 1
                                        print(f"    ✓ Corregido exitosamente (encontrado por snippet)")
                                except Exception:
                                    pass
                            
                            # Estrategia 3: Usar _find_node_by_selector con las estrategias avanzadas
                            if not replaced:
                                try:
                                    found_node = _find_node_by_selector(soup, selector, html_snippet, 0)
                                    if found_node:
                                        found_node.replace_with(new_node)
                                        replaced = True
                                        successful_fixes += 1
                                        print(f"    ✓ Corregido exitosamente (encontrado con estrategias avanzadas)")
                                except Exception:
                                    pass
                            
                            if not replaced:
                                failed_fixes += 1
                                print(f"    ✗ Error: Could not apply fix after multiple attempts")
                                print(f"       Selector: {selector[:100]}")
                else:
                    failed_fixes += 1
                    print(f"    ✗ Error: Could not parse LLM correction")
                    print(f"       Respuesta recibida: {cleaned_response[:200]}...")
            else:
                failed_fixes += 1
                print(f"    ✗ Error: Empty response from LLM")
            
        except Exception as e:
            failed_fixes += 1
            print(f"  > ERROR procesando '{violation.get('selector', '')}': {e}")
    
    print(f"\n[Resumen] Correcciones exitosas: {successful_fixes}, Fallidas: {failed_fixes}")
    
    print(f"\n[Phase 3/3] Restoring responsive design while keeping accessibility fixes...")
    
    try:
        # Pase de seguridad: garantizar botones y enlaces con texto discernible antes del merge responsive
        _ensure_discernible_buttons(soup)
        _ensure_discernible_links(soup)
        current_html = str(soup)

        # Check HTML size to avoid exceeding token limit
        # GPT-4o has a ~128k token limit. If HTML is very large, skip responsive merge
        estimated_tokens = len(original_html) / 4 + len(current_html) / 4  # Approx: 1 token ≈ 4 chars
        if estimated_tokens > 100000:  # Dejar margen de seguridad
            print(f"  ⚠️ HTML too large ({estimated_tokens:.0f} tokens estimated), skipping responsive merge to avoid token limit")
            print(f"  → Usando HTML corregido directamente (las correcciones de accesibilidad se mantienen)")
        else:
            has_screenshots = screenshot_paths is not None and len(screenshot_paths) > 0
            responsive_prompt = _build_responsive_prompt(original_html, current_html, has_screenshots)
            responsive_system = "You are a responsive design expert. MERGE element by element: combine the layout CSS properties from the original HTML with ALL accessibility attributes from the current HTML. NEVER remove aria-*, alt, title, lang, labels, or contrast styles (color, background-color). CRITICAL: Contrast styles (style with 'color:' or 'background-color:') in the CURRENT HTML MUST be preserved COMPLETELY. If an element has contrast styles in the CURRENT one, keep those styles and add the layout styles from the ORIGINAL. The result must have the original's responsive design + all accessibility fixes. CRITICAL: Keep ALL HTML content, including footer, scripts at the end, and any bottom elements. Do NOT remove any part of the HTML. If screenshots are available, the final design MUST look IDENTICAL to the screenshots in terms of layout, sizes, spacing and background colours."
            
            try:
                messages = [
                    {"role": "system", "content": responsive_system},
                ]
                
                # Si hay capturas, incluirlas en el mensaje del usuario
                if has_screenshots:
                    import base64
                    user_content = [{"type": "text", "text": responsive_prompt}]
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
                            print(f"  ⚠️ Error al incluir captura {screenshot_path}: {e}")
                    messages.append({"role": "user", "content": user_content})
                else:
                    messages.append({"role": "user", "content": responsive_prompt})
                
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=messages,
                    temperature=0.0,
                    max_completion_tokens=200000
                )
                
                responsive_html = _extract_clean_html(response.choices[0].message.content)
                validated_soup = _validate_responsive_html(responsive_html, original_html, current_html)
                
                if validated_soup:
                    soup = validated_soup
                    print(f"  ✓ Responsive design restored while keeping accessibility")
                else:
                    print(f"  → Usando HTML actual en lugar del merge")
            except Exception as api_error:
                error_str = str(api_error)
                if "context_length_exceeded" in error_str or "maximum context length" in error_str:
                    print(f"  ⚠️ HTML demasiado grande para el modelo, saltando merge responsive")
                    print(f"  → Usando HTML corregido directamente (las correcciones de accesibilidad se mantienen)")
                else:
                    raise
    except Exception as e:
        print(f"  ⚠️ Error restaurando responsive: {e}. Continuando con HTML actual...")
    
    soup = convert_paths_to_absolute(soup, base_url)
    print("\n--- Correction process finished ---")
    return str(soup)