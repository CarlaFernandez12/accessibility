"""Heuristic HTML fix helpers for images, buttons, links, and fragment context."""

from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def _candidate_image_keys(src_value: Optional[str], base_url: str) -> List[str]:
    """Generate candidate keys for looking up image descriptions."""
    if not src_value:
        return []
    candidates = {src_value}
    try:
        candidates.add(urljoin(base_url, src_value))
        for value in list(candidates):
            parsed = urlparse(value)
            candidates.add(parsed._replace(query='', fragment='').geturl())
    except Exception:
        pass
    return list(candidates)


def _fix_owl_controls(node_to_fix, violation, fixed_dot_containers):
    """Fix Owl Carousel controls with heuristics."""
    violation_id_val = (violation.get('violation_id') or violation.get('id') or '').lower()
    description_val = (violation.get('description') or '').lower()

    class_list = node_to_fix.get('class', [])
    if isinstance(class_list, str):
        class_list = class_list.split()
    role_val = node_to_fix.get('role', '')

    if 'owl-prev' in class_list or ('prev' in class_list and node_to_fix.name == 'button'):
        node_to_fix['aria-label'] = 'Previous slide'
        return True
    if 'owl-next' in class_list or ('next' in class_list and node_to_fix.name == 'button'):
        node_to_fix['aria-label'] = 'Next slide'
        return True
    if 'owl-dot' in class_list:
        dots_container = node_to_fix.find_parent(class_='owl-dots')
        if dots_container and id(dots_container) not in fixed_dot_containers:
            for index, dot in enumerate(dots_container.find_all('button', class_='owl-dot')):
                dot['aria-label'] = f'Go to slide {index + 1}'
            fixed_dot_containers.add(id(dots_container))
        return True
    if (node_to_fix.name == 'button' or role_val == 'button') and not node_to_fix.get('aria-label'):
        label_candidates = {
            'plus': 'Agregar', 'bi-plus': 'Agregar', 'bi-plus-lg': 'Agregar',
            'add': 'Agregar', 'close': 'Cerrar', 'x': 'Cerrar', 'bi-x': 'Cerrar',
            'search': 'Buscar', 'bi-search': 'Buscar', 'delete': 'Eliminar',
            'trash': 'Eliminar', 'bi-trash': 'Eliminar',
        }
        label = node_to_fix.get('title') or node_to_fix.get_text().strip()
        if not label and isinstance(class_list, list):
            joined = ' '.join(class_list).lower()
            for key, value in label_candidates.items():
                if key in joined:
                    label = value
                    break
        if not label:
            label = 'Button'
        node_to_fix['aria-label'] = label
        return True

    return False


def _fix_link_name(node_to_fix, violation):
    """Fix links without discernible text by adding aria-label or visible text."""
    if node_to_fix.name != 'a':
        return False

    has_text = node_to_fix.get_text(strip=True) != ''
    has_aria_label = (node_to_fix.get('aria-label') or '').strip() != ''
    has_title = (node_to_fix.get('title') or '').strip() != ''

    if has_text or has_aria_label:
        return False

    href = node_to_fix.get('href', '')
    title = node_to_fix.get('title', '')
    class_list = node_to_fix.get('class', [])
    if isinstance(class_list, str):
        class_list = class_list.split()

    label = None
    if title:
        label = title
    elif href:
        if href.startswith('http'):
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
            path_parts = href.split('/')
            if path_parts:
                last_part = path_parts[-1].replace('.html', '').replace('.htm', '').replace('-', ' ').replace('_', ' ')
                if last_part:
                    label = f'Enlace a {last_part.title()}'

    if not label and class_list:
        label_candidates = {
            'home': 'Home', 'menu': 'Menu', 'nav': 'Navigation',
            'logo': 'Logo', 'icon': 'Icono', 'social': 'Red social',
            'facebook': 'Facebook', 'twitter': 'Twitter', 'instagram': 'Instagram',
            'linkedin': 'LinkedIn', 'youtube': 'YouTube', 'email': 'Correo',
            'phone': 'Phone', 'contact': 'Contact', 'about': 'About',
            'next': 'Next', 'prev': 'Previous', 'back': 'Back',
            'more': 'More information', 'read': 'Read more', 'download': 'Download',
        }
        joined = ' '.join(class_list).lower()
        for key, value in label_candidates.items():
            if key in joined:
                label = value
                break

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

    if not label:
        parent = node_to_fix.find_parent()
        if parent:
            siblings = parent.find_all('a', limit=5)
            if len(siblings) > 1:
                index = siblings.index(node_to_fix) if node_to_fix in siblings else 0
                label = f'Enlace {index + 1}'
            else:
                label = 'Enlace'
        else:
            label = 'Enlace'

    if label:
        node_to_fix['aria-label'] = label
        return True

    return False


def _process_image_descriptions(soup, media_descriptions, base_url):
    """Apply image descriptions to img tags."""
    for img_tag in soup.find_all('img'):
        src = img_tag.get('src')
        for key in _candidate_image_keys(src, base_url):
            if key in media_descriptions:
                img_tag['alt'] = img_tag['title'] = media_descriptions[key]
                break


def _get_text_elements(node):
    """Return child elements that contain visible text."""
    text_tags = ['p', 'span', 'a', 'li', 'td', 'th', 'label', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'b', 'i']
    text_elements = []
    for tag in text_tags:
        for child in node.find_all(tag, recursive=True):
            if child.get_text(strip=True):
                text_elements.append(child)
    return text_elements


def _get_fragment_images(fragment_html, media_descriptions, base_url):
    """Extract image information from the fragment."""
    fragment_soup = BeautifulSoup(fragment_html, 'html.parser')
    fragment_images = []
    for img in fragment_soup.find_all('img'):
        img_src = img.get('src', '')
        if not img_src:
            continue
        for key in _candidate_image_keys(img_src, base_url):
            if key in media_descriptions:
                fragment_images.append(f"  - {img_src}: {media_descriptions[key]}")
                break
    if fragment_images:
        return (
            "\n**Available image descriptions**:\n"
            + "\n".join(fragment_images)
            + "\nIMPORTANT: If the fragment contains images, use these descriptions for the `alt` and `title` attributes. KEEP these descriptions exact.\n"
        )
    return ""


def _ensure_discernible_buttons(soup):
    """Ensure icon-only buttons have discernible text via aria-label."""
    label_candidates = {
        'bi-plus-lg': 'Agregar', 'bi-plus': 'Agregar', 'plus': 'Agregar', 'add': 'Agregar',
        'bi-x': 'Cerrar', 'x': 'Cerrar', 'close': 'Cerrar',
        'bi-search': 'Buscar', 'search': 'Buscar',
        'bi-trash': 'Eliminar', 'trash': 'Eliminar', 'delete': 'Eliminar',
    }

    buttons = set(soup.find_all('button'))
    buttons.update(soup.find_all(role='button'))

    for btn in buttons:
        has_text = (btn.get_text() or '').strip() != ''
        has_aria_label = (btn.get('aria-label') or '').strip() != ''

        if has_text or has_aria_label:
            continue

        joined_classes = ' '.join(btn.get('class', [])).lower()
        inferred_label = None
        for key, value in label_candidates.items():
            if key in joined_classes:
                inferred_label = value
                break

        if inferred_label:
            final_label = inferred_label
        else:
            title_val = (btn.get('title') or '').strip()
            if title_val:
                final_label = title_val
            else:
                final_label = 'Button'

        btn['aria-label'] = final_label


def _ensure_discernible_links(soup):
    """Ensure links without discernible text have an accessible name."""
    for a_tag in soup.find_all('a'):
        try:
            _fix_link_name(a_tag, {})
        except Exception as error:
            print(f"  Warning: failed to fix link without discernible text: {error}")