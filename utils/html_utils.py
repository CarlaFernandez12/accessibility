"""
Utilities for HTML manipulation.

This module provides functions to convert relative paths to absolute
and other HTML manipulation operations.
"""

from typing import Any
from urllib.parse import urljoin

# Map of HTML tags to their path attributes
_TAG_PATH_ATTRIBUTES = {
    'a': 'href',
    'link': 'href',
    'script': 'src',
    'img': 'src',
    'source': 'src',
    'iframe': 'src',
    'form': 'action',
}

# Prefijos que indican que una ruta ya es absoluta o especial
_ABSOLUTE_PATH_PREFIXES = ('http://', 'https://', '#', 'data:', 'mailto:', 'tel:')


def convert_paths_to_absolute(soup: Any, base_url: str) -> Any:
    """
    Convierte todas las rutas relativas a absolutas en el HTML.
    
    Args:
        soup: Objeto BeautifulSoup con el HTML parseado
        base_url: URL base para convertir rutas relativas
        
    Returns:
        Objeto BeautifulSoup modificado con rutas absolutas
    """
    print("\n[Paso 3/3] Convirtiendo rutas relativas a absolutas...")
    converted_count = 0
    
    for tag_name, attribute_name in _TAG_PATH_ATTRIBUTES.items():
        for tag in soup.find_all(tag_name):
            path = tag.get(attribute_name)
            if path and not path.startswith(_ABSOLUTE_PATH_PREFIXES):
                absolute_path = urljoin(base_url, path)
                tag[attribute_name] = absolute_path
                converted_count += 1
    
    print(f"Se han convertido {converted_count} rutas a absolutas.")
    return soup
