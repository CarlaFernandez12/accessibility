"""
Módulo para generación de HTML accesible.

Este módulo proporciona funciones para corregir errores de accesibilidad
en HTML usando LLMs y técnicas de procesamiento de texto.
"""

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from lxml import etree

from utils.html_utils import convert_paths_to_absolute
from utils.io_utils import log_openai_call
from utils.violation_utils import flatten_violations, prioritize_violations

# Constantes para cálculos de contraste
CONTRAST_RATIO_MAX = 21.0
CONTRAST_ADJUSTMENT = 0.05
LUMINANCE_THRESHOLD = 0.5

# Colores candidatos para contraste
DARK_COLOR_CANDIDATES = [
    '#000000', '#212121', '#424242', '#000080', '#006400',
    '#8B0000', '#4A4A4A', '#2C2C2C'
]
LIGHT_COLOR_CANDIDATES = [
    '#FFFFFF', '#F5F5F5', '#E0E0E0', '#FFD700', '#00FFFF',
    '#FFFF00', '#D3D3D3', '#C0C0C0'
]

# Coeficientes para cálculo de luminancia (WCAG)
LUMINANCE_COEFFICIENTS = {
    'r': 0.2126,
    'g': 0.7152,
    'b': 0.0722
}
LUMINANCE_THRESHOLD_ADJUST = 0.03928
LUMINANCE_ADJUSTMENT_FACTOR = 12.92
LUMINANCE_GAMMA = 2.4

# ============================================================================
# Funciones auxiliares para cálculos de contraste
# ============================================================================

def hex_to_rgb(hex_color):
    """
    Convierte un color hexadecimal a RGB.
    
    Args:
        hex_color: Color en formato hexadecimal (con o sin #)
        
    Returns:
        Tupla (r, g, b) con valores de 0 a 255
    """
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def _adjust_component_luminance(component):
    """
    Ajusta un componente de color para el cálculo de luminancia según WCAG.
    
    Args:
        component: Componente de color normalizado (0.0-1.0)
        
    Returns:
        Componente ajustado para cálculo de luminancia
    """
    if component <= LUMINANCE_THRESHOLD_ADJUST:
        return component / LUMINANCE_ADJUSTMENT_FACTOR
    return ((component + 0.055) / 1.055) ** LUMINANCE_GAMMA


def get_luminance(rgb):
    """
    Calcula la luminosidad relativa según WCAG 2.1.
    
    Args:
        rgb: Tupla (r, g, b) con valores de 0 a 255
        
    Returns:
        Luminancia relativa (0.0-1.0)
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


def calculate_contrast_ratio(color1_hex, color2_hex):
    """
    Calcula el ratio de contraste entre dos colores según WCAG.
    
    Args:
        color1_hex: Primer color en formato hexadecimal
        color2_hex: Segundo color en formato hexadecimal
        
    Returns:
        Ratio de contraste (1.0-21.0)
    """
    lum1 = get_luminance(hex_to_rgb(color1_hex))
    lum2 = get_luminance(hex_to_rgb(color2_hex))
    lighter, darker = max(lum1, lum2), min(lum1, lum2)
    
    if darker == 0:
        return CONTRAST_RATIO_MAX
    
    return (lighter + CONTRAST_ADJUSTMENT) / (darker + CONTRAST_ADJUSTMENT)


def find_contrasting_color(bg_color_hex, required_ratio):
    """
    Encuentra un color que cumpla el ratio de contraste requerido.
    
    Args:
        bg_color_hex: Color de fondo en formato hexadecimal
        required_ratio: Ratio de contraste requerido (ej: "4.5:1")
        
    Returns:
        Color que cumple el contraste requerido
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

def _candidate_image_keys(src_value, base_url):
    """Genera claves candidatas para buscar descripciones de imágenes"""
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

def _normalize_angular_selector(selector):
    """Normaliza un selector CSS eliminando atributos Angular dinámicos (_ngcontent-*, _nghost-*)"""
    if not selector:
        return selector
    
    # Eliminar atributos Angular del selector: [attr="_ngcontent-xxx"] o [_ngcontent-xxx]
    normalized = re.sub(r'\[_ngcontent-[^\]]+\]', '', selector)
    normalized = re.sub(r'\[_nghost-[^\]]+\]', '', normalized)
    normalized = re.sub(r'\[attr="_ngcontent-[^"]+"\]', '', normalized)
    normalized = re.sub(r'\[attr="_nghost-[^"]+"\]', '', normalized)
    
    # Limpiar espacios múltiples
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    
    return normalized

def _normalize_angular_html(html_str):
    """Normaliza HTML eliminando atributos Angular dinámicos para comparación"""
    if not html_str:
        return html_str
    
    # Eliminar atributos _ngcontent-* y _nghost-*
    normalized = re.sub(r'\s+_ngcontent-[^=]*="[^"]*"', '', html_str)
    normalized = re.sub(r'\s+_nghost-[^=]*="[^"]*"', '', normalized)
    
    return normalized

def _css_to_xpath(css_selector):
    """Convierte un selector CSS a XPath básico"""
    if not css_selector:
        return None
    
    # Normalizar selector Angular primero
    css_selector = _normalize_angular_selector(css_selector)
    
    # Limpiar selector de pseudo-clases problemáticas primero
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
            # Es un atributo, añadirlo al último tag
            if xpath_parts:
                xpath_parts[-1] += xpath_part
            else:
                xpath_parts.append('*' + xpath_part)
    
    # Construir el XPath final
    if not xpath_parts:
        return '//*'
    
    # Determinar separador basado en si había >
    separator = '//' if '>' not in css_selector else '/'
    result = separator + separator.join(xpath_parts)
    
    return result

def _find_node_by_html_snippet(soup, html_snippet):
    """Encuentra un nodo comparando su HTML con el snippet de la violación, ignorando atributos Angular"""
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
                    
                    # Si hay atributos en común o el snippet es muy similar
                    if snippet_attrs.intersection(element_attrs) or len(snippet_clean) > 50:
                        return element
    
    return None

def _find_node_by_selector(soup, selector, html_snippet=None, violation_index=0):
    """Intenta encontrar un nodo usando múltiples estrategias: CSS, XPath, y HTML snippet matching, con soporte para Angular"""
    # Normalizar selector Angular primero
    normalized_selector = _normalize_angular_selector(selector)
    
    # Estrategia 1: Intentar con CSS selector normalizado (sin atributos Angular)
    try:
        nodes = soup.select(normalized_selector)
        if nodes:
            # Si hay múltiples nodos, usar el índice de la violación o el HTML snippet para encontrar el correcto
            if len(nodes) == 1:
                return nodes[0]
            elif html_snippet:
                # Normalizar HTML snippet para comparación
                snippet_clean = _normalize_angular_html(html_snippet)
                # Si hay múltiples nodos, usar el HTML snippet para encontrar el correcto
                for node in nodes:
                    node_html = str(node)
                    node_clean = _normalize_angular_html(node_html)
                    if snippet_clean in node_clean or node_clean in snippet_clean:
                        return node
                # Si no se encuentra por snippet, devolver el primero
                return nodes[0] if nodes else None
            else:
                # Si no hay snippet, devolver el nodo en el índice de la violación
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
                    # Buscar entre múltiples nodos usando el snippet
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
                    # Sin snippet, usar el nodo en el índice de la violación
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
            # Intentar encontrar por la última clase (el elemento objetivo)
            target_class = class_matches[-1]
            nodes = soup.find_all(class_=re.compile(f'\\b{re.escape(target_class)}\\b'))
            if nodes and html_snippet:
                # Si hay snippet, buscar el que más coincida
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
            for attr_match in reversed(attr_matches):  # Empezar por el último
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
    
    # Estrategia 6: Último recurso - selector simplificado (última parte)
    try:
        last_part = selector.split('>')[-1].strip()
        last_part = re.sub(r':[a-z-]+(\([^)]+\))?', '', last_part)
        if last_part:
            nodes = soup.select(last_part)
            if nodes:
                if html_snippet:
                    # Intentar encontrar el que más coincida con el snippet
                    snippet_clean = _normalize_angular_html(html_snippet)
                    for node in nodes:
                        node_html = str(node)
                        node_clean = _normalize_angular_html(node_html)
                        if snippet_clean[:50] in node_clean or node_clean[:50] in snippet_clean:
                            return node
                return nodes[0]
    except Exception:
        pass
    
    # Estrategia 7: Buscar por tag name del último elemento del selector
    try:
        # Extraer el tag name del último elemento
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
                    # Buscar coincidencias más estrictas
                    if snippet_clean in node_clean or node_clean in snippet_clean:
                        return node
                    # O al menos buscar por contenido de texto si es pequeño
                    if len(snippet_clean) < 200 and node.get_text(strip=True) in snippet_clean:
                        return node
    except Exception:
        pass
    
    return None

def _fix_owl_controls(node_to_fix, violation, fixed_dot_containers):
    """Corrige controles de Owl Carousel con heurísticas"""
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
    """Corrige enlaces sin texto discernible añadiendo aria-label o texto visible"""
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
            'home': 'Inicio', 'menu': 'Menú', 'nav': 'Navegación',
            'logo': 'Logo', 'icon': 'Icono', 'social': 'Red social',
            'facebook': 'Facebook', 'twitter': 'Twitter', 'instagram': 'Instagram',
            'linkedin': 'LinkedIn', 'youtube': 'YouTube', 'email': 'Correo',
            'phone': 'Teléfono', 'contact': 'Contacto', 'about': 'Acerca de',
            'next': 'Siguiente', 'prev': 'Anterior', 'back': 'Atrás',
            'more': 'Más información', 'read': 'Leer más', 'download': 'Descargar'
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
                label = 'Teléfono'
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
    
    # 5. Si aún no hay label, usar uno genérico basado en el contexto
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
    """Prompt compacto para corrección de contraste en HTML."""
    description = violation.get('description', 'Error de contraste de color')
    failure_summary = violation.get('failure_summary', '')

    screenshot_note = ""
    if has_screenshots:
        screenshot_note = "Usa las capturas solo como referencia visual; no cambies el layout ni los fondos."

    parts = [
        f"Corrige ESTE error de contraste de color en el siguiente fragmento HTML.",
        "",
        f"VIOLACIÓN: {description}",
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
    parts.append("REGLAS RÁPIDAS:")
    parts.append(f"- Ajusta SOLO el color del texto: style=\"color: {recommended_color_str}\"")
    parts.append("- Mantén fondos y layout tal como están (no cambies tamaños ni posiciones).")
    parts.append("- Si hay elementos hijos con texto, aplica también el nuevo color de texto a esos elementos.")

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
        screenshot_note = "Mantén el aspecto visual que se ve en las capturas (layout, colores, responsive)."

    lines = [
        "Corrige ESTE error de accesibilidad en el siguiente fragmento HTML.",
        "",
        f"VIOLACIÓN: {description}",
    ]
    if failure_summary:
        lines.append(f"DETALLE: {failure_summary}")
    if help_text:
        lines.append(f"AYUDA (Axe): {help_text}")
    if help_url:
        lines.append(f"Más info: {help_url}")
    if images_info:
        lines.append(images_info.strip())
    if screenshot_note:
        lines.append(screenshot_note)

    lines.append("")
    lines.append("REGLAS RÁPIDAS (según el tipo de error):")
    lines.append("- button-name / link-name → añade texto visible o aria-label=\"...\".")
    lines.append("- image-alt / role-img-alt → añade alt=\"...\" o aria-label=\"...\".")
    lines.append("- aria-* → añade/corrige atributos aria- (aria-label, aria-labelledby, role, etc.).")
    lines.append("- focus / keyboard → asegúrate de que el elemento es focuseable y operable con teclado.")

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
    """Aplica descripciones de imágenes a las etiquetas img"""
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
    """Extrae información de imágenes del fragmento"""
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
        return f"\n**Descripciones de imágenes disponibles**:\n" + "\n".join(fragment_images) + "\nIMPORTANTE: Si el fragmento contiene imágenes, usa estas descripciones para los atributos `alt` y `title`. MANTÉN estas descripciones exactas.\n"
    return ""

    



def _calculate_contrast_info(violation):
    """Calcula información de contraste y genera recomendaciones"""
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
**INFORMACIÓN DE CONTRASTE DETECTADA**:
- Color de fondo actual: {bg_color}
- Color de texto actual: {fg_color}
- Ratio de contraste actual: {current_ratio}
- Ratio de contraste requerido: {required_ratio}
- Tamaño de fuente: {font_size}
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
                color_suggestions = f"**COLOR RECOMENDADO**: {recommended_color} - garantiza contraste máximo\n"
            except:
                color_suggestions = "**COLOR RECOMENDADO**: #000000 (negro) - color seguro para la mayoría de fondos claros\n"
    
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
- Ejemplo: Si tienes `<div><p>Texto</p><span>Más texto</span></div>`, el resultado debe ser:
  `<div style="color: {recommended_color_str}"><p style="color: {recommended_color_str}">Texto</p><span style="color: {recommended_color_str}">Más texto</span></div>`
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
    """Construye el prompt para restaurar diseño responsive"""
    screenshot_instructions = ""
    if has_screenshots:
        screenshot_instructions = """
🚨 CRÍTICO - REFERENCIA VISUAL:
He incluido capturas de pantalla que muestran cómo se ve REALMENTE la página en diferentes tamaños (mobile, tablet, desktop) ANTES de las correcciones.

**INSTRUCCIONES OBLIGATORIAS**:
1. EXAMINA DETALLADAMENTE cada captura para entender el diseño visual REAL
2. El diseño final DEBE verse IDÉNTICO a las capturas en términos de:
   - Layout y distribución de elementos
   - Tamaños y espaciado
   - Colores de fondo (NO cambies los que se ven en las capturas)
   - Responsive behavior (cómo se adapta en mobile/tablet/desktop)
3. MANTÉN todas las correcciones de accesibilidad (aria-label, alt, roles, estilos de contraste)
4. El resultado debe ser: diseño de las capturas + correcciones de accesibilidad invisibles

"""
    
    return f"""Eres un experto en diseño web responsive. HAZ UN MERGE INTELIGENTE: combina el diseño responsive del HTML original con las correcciones de accesibilidad del HTML actual.
{screenshot_instructions}

## OBJETIVO CRÍTICO:
Hacer un MERGE elemento por elemento:
- Del HTML ORIGINAL: tomar SOLO las propiedades CSS de layout (width, height, position, display, flex, grid, margin, padding, unidades)
- Del HTML ACTUAL: mantener TODOS los atributos de accesibilidad (aria-label, lang, alt, title, labels, roles ARIA, style="color:...")

## PROCESO DE MERGE (elemento por elemento):
1. Para cada elemento en el HTML actual, encuentra el elemento correspondiente en el HTML original (por selector/clase/id)
2. Del elemento ORIGINAL: copia SOLO las propiedades CSS de layout en el atributo `style` o `class`
3. Del elemento ACTUAL: mantén TODOS estos atributos de accesibilidad:
   - aria-label, aria-labelledby, aria-describedby, aria-current, role
   - lang
   - alt, title (en imágenes)
   - id (si se usó para asociar labels)
   - style="color: ..." (estilos de contraste) - CRÍTICO: NUNCA elimines estos
   - style="background-color: ..." (si se añadió para corregir contraste) - CRÍTICO: NUNCA elimines estos
   - CUALQUIER atributo style que contenga "color:" o "background-color:" - CRÍTICO: NUNCA elimines estos
   - label for="..." (si se añadieron labels)
   - Todos los atributos ARIA que se hayan añadido
4. Combina ambos: el elemento final debe tener las propiedades CSS del original + todos los atributos de accesibilidad del actual
5. Si un elemento tiene style="color: ..." o style="background-color: ..." en el HTML ACTUAL, DEBES preservarlo COMPLETAMENTE en el resultado final

## REGLAS ESTRICTAS:
1. NUNCA elimines atributos que empiecen con "aria-"
2. NUNCA elimines atributos "alt", "title", "lang"
3. NUNCA elimines elementos `<label>` que se hayan añadido
4. NUNCA elimines estilos `style="color: ..."` que se hayan añadido - CRÍTICO PARA CONTRASTE
5. NUNCA elimines estilos `style="background-color: ..."` que se hayan añadido para contraste - CRÍTICO
6. Si un elemento tiene `style` con "color:" o "background-color:" en el HTML ACTUAL, DEBES preservarlo COMPLETAMENTE, incluso si combinas con otros estilos del original
7. SIEMPRE mantén las clases del original (pueden tener CSS responsivo)
8. SIEMPRE mantén las propiedades CSS de layout del original (width, height, position, display, flex, grid, margin, padding)
9. Al combinar estilos, SIEMPRE preserva primero los estilos de contraste (color, background-color) del HTML ACTUAL, luego añade los estilos de layout del original

## PROHIBIDO:
• NO elimines correcciones de accesibilidad
• NO restaures atributos que eliminen las correcciones
• NO cambies el diseño responsive original (solo combínalo con las correcciones)

## HTML ORIGINAL (referencia para diseño responsive):
```html
{original_html}
```

## HTML ACTUAL (con correcciones de accesibilidad):
```html
{current_html}
```

⚠️ CRÍTICO - IMPORTANTE:
1. Ambos HTML deben estar COMPLETOS en el prompt - NO elimines ninguna parte
2. Debes procesar TODO el contenido desde el inicio hasta el final
3. Debes incluir footer, scripts al final, y cualquier elemento de la parte inferior
4. El HTML resultante DEBE tener al menos el 95% de la longitud del HTML original
5. Si el HTML original tiene 100,000 caracteres, el resultado debe tener al menos 95,000 caracteres
6. NO cortes el HTML por la mitad - debe estar COMPLETO

**VERIFICACIÓN REQUERIDA**: Antes de responder, verifica que tu respuesta tenga aproximadamente la misma longitud que el HTML original. Si tu respuesta es significativamente más corta, significa que cortaste contenido y debes volver a generar el HTML completo.

Devuelve el HTML COMPLETO haciendo el MERGE: diseño responsive del original + TODAS las correcciones de accesibilidad del actual. El HTML resultante DEBE tener la misma longitud y estructura completa que el original."""
        
def _validate_responsive_html(responsive_html, original_html, current_html):
    """Valida y procesa el HTML responsive resultante"""
    if not responsive_html or "<html" not in responsive_html.lower():
        return None
    
    original_length = len(original_html)
    responsive_length = len(responsive_html)
    length_ratio = responsive_length / original_length if original_length > 0 else 0
    
    soup = BeautifulSoup(responsive_html, 'html.parser')
    body = soup.find('body')
    
    if body and len(body.get_text().strip()) >= 100:
        if length_ratio < 0.7:
            print(f"  ⚠️ ADVERTENCIA: El HTML resultante es significativamente más corto ({length_ratio:.1%} del original)")
        return soup
    else:
        print("  ⚠️ ADVERTENCIA: El body parece estar vacío o truncado")
        return None

def _ensure_discernible_buttons(soup):
    """Asegura que los botones icon-only tengan texto discernible vía aria-label."""
    print("--- [DEBUG] Iniciando _ensure_discernible_buttons (v2) ---")
    label_candidates = {
        'bi-plus-lg': 'Agregar', 'bi-plus': 'Agregar', 'plus': 'Agregar', 'add': 'Agregar',
        'bi-x': 'Cerrar', 'x': 'Cerrar', 'close': 'Cerrar',
        'bi-search': 'Buscar', 'search': 'Buscar',
        'bi-trash': 'Eliminar', 'trash': 'Eliminar', 'delete': 'Eliminar',
    }
    
    buttons = set(soup.find_all('button'))
    buttons.update(soup.find_all(role='button'))
    print(f"  > [DEBUG] Encontrados {len(buttons)} elementos de tipo botón.")

    for btn in buttons:
        classes_str = ' '.join(btn.get('class', []))
        
        # 1. Comprobar si tiene texto visible
        has_text = (btn.get_text() or '').strip() != ''
        
        # 2. Comprobar si tiene un aria-label existente y no vacío
        has_aria_label = (btn.get('aria-label') or '').strip() != ''
        
        # Si tiene texto O un aria-label válido, está bien. Pasamos al siguiente.
        if has_text or has_aria_label:
            if has_text:
                print(f"\n  > [DEBUG] SALTANDO (tiene texto): <{btn.name} class='{classes_str}'>")
            if has_aria_label:
                print(f"\n  > [DEBUG] SALTANDO (ya tiene aria-label): <{btn.name} class='{classes_str}' aria-label='{btn.get('aria-label')}'>")
            continue

        # Si estamos aquí, es un botón-icono SIN texto y SIN aria-label.
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
            # 4. Si no se infiere, usar el 'title' si existe y no está vacío
            title_val = (btn.get('title') or '').strip()
            if title_val:
                final_label = title_val
                print(f"    > Usando 'title' existente: '{final_label}'")
            else:
                # 5. Como último recurso, usar una etiqueta genérica
                final_label = 'Button'
                print(f"    > Usando etiqueta por defecto: '{final_label}'")

        print(f"    > RESULTADO: APLICANDO ETIQUETA '{final_label}'")
        btn['aria-label'] = final_label
    
    print("--- [DEBUG] Finalizado _ensure_discernible_buttons (v2) ---")

def generate_accessible_html_with_parser(original_html, axe_results, media_descriptions, client, base_url, driver, screenshot_paths=None):
    print("\n--- Iniciando Proceso de Corrección Solo con LLMs ---")
    
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
            print(f"     - {v_type}: {count} violación(es)")
    
    for violation in violations_to_fix:
        try:
            selector = violation.get('selector', '')
            html_snippet = violation.get('html_snippet', '')
            violation_id = violation.get('violation_id', 'unknown')

            # Normalizar selector Angular primero
            normalized_selector = _normalize_angular_selector(selector)

            # Intentar encontrar el elemento - método original simple que funcionaba mejor
            node_to_fix = None
            try:
                # Método 1: Intentar con selector normalizado (sin atributos Angular)
                node_to_fix = soup.select_one(normalized_selector)
            except Exception:
                pass

            # Método 2: Intentar con selector original (por si acaso)
            if not node_to_fix:
                try:
                    node_to_fix = soup.select_one(selector)
                except Exception:
                    pass

            # Si no se encontró, intentar con select (método original alternativo)
            if not node_to_fix:
                try:
                    nodes = soup.select(normalized_selector)
                    if not nodes:
                        nodes = soup.select(selector)
                    if nodes:
                        # Si hay múltiples, usar el HTML snippet normalizado para encontrar el correcto
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

            # Si aún no se encontró, intentar con selector simplificado (método original)
            if not node_to_fix:
                try:
                    simplified = re.sub(r':nth-child\([^)]+\)|:first-child|:last-child|:nth-of-type\([^)]+\)', '', normalized_selector).strip()
                    if simplified:
                        node_to_fix = soup.select_one(simplified)
                except Exception:
                    pass

            # Si aún no se encontró, usar función mejorada con XPath (incluye normalización Angular)
            if not node_to_fix:
                node_to_fix = _find_node_by_selector(soup, selector, html_snippet, 0)

            # Último intento: buscar por HTML snippet directamente
            if not node_to_fix:
                print(f"  ⚠️ No se encontró elemento para selector: {selector[:50]}... (intentando con HTML snippet y estrategias avanzadas)")
                if html_snippet:
                    node_to_fix = _find_node_by_html_snippet(soup, html_snippet)

                if not node_to_fix:
                    # Las estrategias avanzadas (5-7) ya fueron intentadas en _find_node_by_selector (línea 1038)
                    # que incluyen búsqueda por clases, IDs y atributos extraídos del selector
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
                system_message = f"Eres un experto en accesibilidad. CORRIGE el contraste de color añadiendo el atributo style con color: {recommended_color}. Es OBLIGATORIO corregir este error. Si hay elementos hijos con texto, aplica el estilo también a ellos. NO añadas otros atributos innecesarios. MANTÉN el diseño responsive tal como aparece en las capturas (si están disponibles)."
            else:
                prompt = _build_general_prompt(violation, original_fragment, images_info, has_screenshots)
                system_message = "Eres un experto en accesibilidad web. Tu PRIORIDAD es corregir TODOS los errores de accesibilidad mencionados MANTENIENDO el diseño responsive que se ve en las capturas. Las correcciones deben ser 'invisibles' visualmente (usa aria-label, roles, alt text). NO añadas comentarios HTML ni atributos que muestren que fueron correcciones. El HTML debe verse como si fuera código original, no corregido."
            
            corrected_fragment_str = _call_llm_for_fix(client, prompt, system_message, screenshot_paths)
            log_openai_call(prompt=prompt, response=corrected_fragment_str, model="gpt-4o", call_type="html_fix")
            
            if corrected_fragment_str:
                # Limpiar posible código markdown alrededor de la respuesta
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
                        # Buscar el primer tag HTML válido
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
                    
                    # Si son idénticos después de normalizar, el LLM no hizo cambios
                    if original_normalized.strip() == new_normalized.strip():
                        failed_fixes += 1
                        print(f"    ✗ Error: El LLM devolvió el mismo código sin correcciones")
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
                                    # Buscar el nodo que más coincida con el original
                                    for candidate_node in nodes:
                                        try:
                                            candidate_normalized = _normalize_angular_html(str(candidate_node))
                                            if original_normalized[:100] in candidate_normalized or candidate_normalized[:100] in original_normalized:
                                                candidate_node.replace_with(new_node)
                                                replaced = True
                                                successful_fixes += 1
                                                print(f"    ✓ Corregido exitosamente (después de reintento)")
                                                break
                                        except Exception:
                                            continue
                                    # Si no se encontró coincidencia pero hay nodos, usar el primero
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
                                print(f"    ✗ Error: No se pudo aplicar la corrección después de múltiples intentos")
                                print(f"       Selector: {selector[:100]}")
                else:
                    failed_fixes += 1
                    print(f"    ✗ Error: No se pudo parsear la corrección del LLM")
                    print(f"       Respuesta recibida: {cleaned_response[:200]}...")
            else:
                failed_fixes += 1
                print(f"    ✗ Error: Respuesta vacía del LLM")
            
        except Exception as e:
            failed_fixes += 1
            print(f"  > ERROR procesando '{violation.get('selector', '')}': {e}")
    
    print(f"\n[Resumen] Correcciones exitosas: {successful_fixes}, Fallidas: {failed_fixes}")
    
    print(f"\n[Fase 3/3] Restaurando diseño responsive manteniendo correcciones de accesibilidad...")
    
    try:
        # Pase de seguridad: garantizar botones con texto discernible antes del merge responsive
        _ensure_discernible_buttons(soup)
        current_html = str(soup)

        # Verificar el tamaño del HTML para evitar exceder el límite de tokens
        # GPT-4o tiene un límite de ~128k tokens. Si el HTML es muy grande, saltar el merge responsive
        estimated_tokens = len(original_html) / 4 + len(current_html) / 4  # Aproximación: 1 token ≈ 4 caracteres
        if estimated_tokens > 100000:  # Dejar margen de seguridad
            print(f"  ⚠️ HTML demasiado grande ({estimated_tokens:.0f} tokens estimados), saltando merge responsive para evitar límite de tokens")
            print(f"  → Usando HTML corregido directamente (las correcciones de accesibilidad se mantienen)")
        else:
            has_screenshots = screenshot_paths is not None and len(screenshot_paths) > 0
            responsive_prompt = _build_responsive_prompt(original_html, current_html, has_screenshots)
            responsive_system = "Eres un experto en diseño responsive. HAZ UN MERGE elemento por elemento: combina las propiedades CSS de layout del HTML original con TODOS los atributos de accesibilidad del HTML actual. NUNCA elimines atributos aria-*, alt, title, lang, labels, o estilos de contraste (color, background-color). CRÍTICO: Los estilos de contraste (style con 'color:' o 'background-color:') del HTML ACTUAL DEBEN preservarse COMPLETAMENTE. Si un elemento tiene estilos de contraste en el ACTUAL, preserva esos estilos y añade los estilos de layout del ORIGINAL. El resultado debe tener el diseño responsive del original + todas las correcciones de accesibilidad. CRÍTICO: Mantén TODO el contenido del HTML, incluyendo footer, scripts al final, y cualquier elemento de la parte inferior. NO elimines ninguna parte del HTML. Si hay capturas disponibles, el diseño final DEBE verse IDÉNTICO a las capturas en términos de layout, tamaños, espaciado y colores de fondo."
            
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
                    print(f"  ✓ Diseño responsive restaurado manteniendo accesibilidad")
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
    print("\n--- Proceso de Corrección Finalizado ---")
    return str(soup)