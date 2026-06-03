"""DOM lookup and replacement helpers for HTML accessibility fixes."""

import re
from typing import Optional

from bs4 import BeautifulSoup
from lxml import etree


def _normalize_angular_selector(selector: str) -> str:
    """
    Normalize a CSS selector by stripping Angular-specific runtime attributes.

    This is useful when selectors include `_ngcontent-*` or `_nghost-*`
    attributes injected by Angular, which do not exist in the original templates.
    """
    if not selector:
        return selector

    normalized = re.sub(r'\[_ngcontent-[^\]]+\]', '', selector)
    normalized = re.sub(r'\[_nghost-[^\]]+\]', '', normalized)
    normalized = re.sub(r'\[attr="_ngcontent-[^"]+"\]', '', normalized)
    normalized = re.sub(r'\[attr="_nghost-[^"]+"\]', '', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def _normalize_angular_html(html_str: Optional[str]) -> Optional[str]:
    """Normalize HTML by stripping Angular runtime attributes for comparison."""
    if not html_str:
        return html_str

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

    css_selector = _normalize_angular_selector(css_selector)

    xpath = re.sub(r':nth-child\([^)]+\)', '', css_selector)
    xpath = re.sub(r':first-child', '', xpath)
    xpath = re.sub(r':last-child', '', xpath)
    xpath = re.sub(r':nth-of-type\([^)]+\)', '', xpath)
    xpath = re.sub(r':hover', '', xpath)
    xpath = re.sub(r':focus', '', xpath)
    xpath = re.sub(r':active', '', xpath)

    parts = []
    current_part = []

    index = 0
    while index < len(xpath):
        char = xpath[index]
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
        index += 1

    if current_part:
        parts.append(''.join(current_part).strip())

    xpath_parts = []
    for part in parts:
        if part == '>' or not part:
            continue

        xpath_part = part
        xpath_part = re.sub(r'#([a-zA-Z0-9_-]+)', r"[@id='\1']", xpath_part)
        xpath_part = re.sub(r'\.([a-zA-Z0-9_-]+)', r"[contains(@class, '\1')]", xpath_part)

        if not xpath_part.startswith('[') and not xpath_part.startswith('//'):
            xpath_parts.append(xpath_part)
        elif xpath_parts:
            xpath_parts[-1] += xpath_part
        else:
            xpath_parts.append('*' + xpath_part)

    if not xpath_parts:
        return '//*'

    separator = '//' if '>' not in css_selector else '/'
    return separator + separator.join(xpath_parts)


def _find_node_by_html_snippet(soup, html_snippet):
    """Find a node by comparing its HTML to the violation snippet, ignoring Angular attributes."""
    if not html_snippet or html_snippet == 'No HTML snippet':
        return None

    snippet_clean = _normalize_angular_html(html_snippet)
    snippet_clean = re.sub(r'\s+', ' ', snippet_clean.strip())

    for element in soup.find_all(True):
        element_html = str(element)
        element_clean = _normalize_angular_html(element_html)
        element_clean = re.sub(r'\s+', ' ', element_clean.strip())

        if snippet_clean in element_clean or element_clean in snippet_clean:
            snippet_soup = BeautifulSoup(html_snippet, 'html.parser')
            snippet_tag = snippet_soup.find()

            if snippet_tag and element.name == snippet_tag.name:
                snippet_attrs = {key for key in snippet_tag.attrs.keys() if not key.startswith('_ng')}
                element_attrs = {key for key in element.attrs.keys() if not key.startswith('_ng')}
                if snippet_attrs.intersection(element_attrs) or len(snippet_clean) > 50:
                    return element

    return None


def _find_node_by_selector(soup, selector, html_snippet=None, violation_index=0):
    """Find a node using multiple CSS/XPath/snippet fallback strategies, with Angular support."""
    normalized_selector = _normalize_angular_selector(selector)

    try:
        nodes = soup.select(normalized_selector)
        if nodes:
            if len(nodes) == 1:
                return nodes[0]
            if html_snippet:
                snippet_clean = _normalize_angular_html(html_snippet)
                for node in nodes:
                    node_clean = _normalize_angular_html(str(node))
                    if snippet_clean in node_clean or node_clean in snippet_clean:
                        return node
                return nodes[0]
            return nodes[violation_index % len(nodes)]
    except Exception:
        pass

    try:
        nodes = soup.select(selector)
        if nodes:
            if len(nodes) == 1:
                return nodes[0]
            if html_snippet:
                snippet_clean = _normalize_angular_html(html_snippet)
                for node in nodes:
                    node_clean = _normalize_angular_html(str(node))
                    if snippet_clean in node_clean or node_clean in snippet_clean:
                        return node
                return nodes[0]
            return nodes[violation_index % len(nodes)]
    except Exception:
        pass

    try:
        simplified = re.sub(
            r':nth-child\([^)]+\)|:first-child|:last-child|:nth-of-type\([^)]+\)',
            '',
            normalized_selector,
        ).strip()
        if simplified:
            nodes = soup.select(simplified)
            if nodes:
                if html_snippet:
                    snippet_clean = _normalize_angular_html(html_snippet)
                    for node in nodes:
                        node_clean = _normalize_angular_html(str(node))
                        if snippet_clean in node_clean or node_clean in snippet_clean:
                            return node
                return nodes[0]
    except Exception:
        pass

    try:
        html_str = str(soup)
        parser = etree.HTMLParser()
        tree = etree.fromstring(html_str.encode('utf-8'), parser)

        xpath = _css_to_xpath(selector)
        if xpath:
            nodes = tree.xpath(xpath)
            if nodes:
                if len(nodes) == 1:
                    node_xml = etree.tostring(nodes[0], encoding='unicode', method='html')
                    if html_snippet and html_snippet in node_xml:
                        node_soup = BeautifulSoup(node_xml, 'html.parser')
                        found = node_soup.find()
                        if found:
                            candidates = soup.find_all(found.name)
                            for candidate in candidates:
                                if set(found.attrs.keys()) == set(candidate.attrs.keys()):
                                    return candidate
                            return soup.find(found.name, found.attrs) if found else None
                    node_soup = BeautifulSoup(node_xml, 'html.parser')
                    found = node_soup.find()
                    if found:
                        candidates = soup.find_all(found.name)
                        if candidates:
                            return candidates[0]
                elif html_snippet:
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
                elif violation_index < len(nodes):
                    node_xml = etree.tostring(nodes[violation_index], encoding='unicode', method='html')
                    node_soup = BeautifulSoup(node_xml, 'html.parser')
                    found = node_soup.find()
                    if found:
                        candidates = soup.find_all(found.name)
                        if candidates:
                            return candidates[violation_index % len(candidates)]
    except Exception:
        pass

    if html_snippet:
        found = _find_node_by_html_snippet(soup, html_snippet)
        if found:
            return found

    try:
        class_matches = re.findall(r'\.([a-zA-Z0-9_-]+)', selector)
        if class_matches:
            target_class = class_matches[-1]
            nodes = soup.find_all(class_=re.compile(f'\\b{re.escape(target_class)}\\b'))
            if nodes and html_snippet:
                snippet_clean = _normalize_angular_html(html_snippet)
                for node in nodes:
                    node_clean = _normalize_angular_html(str(node))
                    if snippet_clean[:50] in node_clean or node_clean[:50] in snippet_clean:
                        return node
            if nodes:
                return nodes[0]

        id_matches = re.findall(r'#([a-zA-Z0-9_-]+)', selector)
        if id_matches:
            node = soup.find(id=id_matches[-1])
            if node:
                return node

        attr_matches = re.findall(r'\[([^\]]+)\]', selector)
        if attr_matches:
            for attr_match in reversed(attr_matches):
                if '=' not in attr_match:
                    continue
                attr_name, attr_value = attr_match.split('=', 1)
                attr_value = attr_value.strip('"\'')
                nodes = soup.find_all(attrs={attr_name: attr_value})
                if nodes:
                    if html_snippet:
                        snippet_clean = _normalize_angular_html(html_snippet)
                        for node in nodes:
                            node_clean = _normalize_angular_html(str(node))
                            if snippet_clean[:50] in node_clean or node_clean[:50] in snippet_clean:
                                return node
                    return nodes[0]
    except Exception:
        pass

    try:
        last_part = selector.split('>')[-1].strip()
        last_part = re.sub(r':[a-z-]+(\([^)]+\))?', '', last_part)
        if last_part:
            nodes = soup.select(last_part)
            if nodes:
                if html_snippet:
                    snippet_clean = _normalize_angular_html(html_snippet)
                    for node in nodes:
                        node_clean = _normalize_angular_html(str(node))
                        if snippet_clean[:50] in node_clean or node_clean[:50] in snippet_clean:
                            return node
                return nodes[0]
    except Exception:
        pass

    try:
        last_part = selector.split('>')[-1].strip()
        tag_name = re.sub(r'[.:#\[].*', '', last_part).strip()
        if tag_name and tag_name[0].isalpha():
            nodes = soup.find_all(tag_name)
            if nodes and html_snippet:
                snippet_clean = _normalize_angular_html(html_snippet)
                for node in nodes:
                    node_clean = _normalize_angular_html(str(node))
                    if snippet_clean in node_clean or node_clean in snippet_clean:
                        return node
                    if len(snippet_clean) < 200 and node.get_text(strip=True) in snippet_clean:
                        return node
    except Exception:
        pass

    return None


def _locate_node_to_fix(soup, selector: str, html_snippet: str):
    """Locate the DOM node to fix using the existing selector and snippet fallback chain."""
    normalized_selector = _normalize_angular_selector(selector)

    try:
        node_to_fix = soup.select_one(normalized_selector)
        if node_to_fix:
            return node_to_fix
    except Exception:
        pass

    try:
        node_to_fix = soup.select_one(selector)
        if node_to_fix:
            return node_to_fix
    except Exception:
        pass

    try:
        nodes = soup.select(normalized_selector)
        if not nodes:
            nodes = soup.select(selector)
        if nodes:
            if len(nodes) == 1:
                return nodes[0]
            if html_snippet:
                snippet_clean = _normalize_angular_html(html_snippet)
                for node in nodes:
                    node_clean = _normalize_angular_html(str(node))
                    if snippet_clean[:100] in node_clean or node_clean[:100] in snippet_clean:
                        return node
            return nodes[0]
    except Exception:
        pass

    try:
        simplified = re.sub(
            r':nth-child\([^)]+\)|:first-child|:last-child|:nth-of-type\([^)]+\)',
            '',
            normalized_selector,
        ).strip()
        if simplified:
            node_to_fix = soup.select_one(simplified)
            if node_to_fix:
                return node_to_fix
    except Exception:
        pass

    node_to_fix = _find_node_by_selector(soup, selector, html_snippet, 0)
    if node_to_fix:
        return node_to_fix

    if html_snippet:
        return _find_node_by_html_snippet(soup, html_snippet)

    return None


def _apply_corrected_node_with_fallbacks(soup, selector: str, html_snippet: str, new_node, original_normalized: str) -> bool:
    """Apply a corrected DOM node using the existing replacement fallback chain."""
    try:
        node_to_fix = _locate_node_to_fix(soup, selector, html_snippet)
        if node_to_fix:
            node_to_fix.replace_with(new_node)
            return True
    except Exception:
        pass

    try:
        nodes = soup.select(selector)
        if not nodes:
            normalized_sel = _normalize_angular_selector(selector)
            nodes = soup.select(normalized_sel)
        if nodes:
            for candidate_node in nodes:
                try:
                    candidate_normalized = _normalize_angular_html(str(candidate_node))
                    if original_normalized[:100] in candidate_normalized or candidate_normalized[:100] in original_normalized:
                        candidate_node.replace_with(new_node)
                        return True
                except Exception:
                    continue
            nodes[0].replace_with(new_node)
            return True
    except Exception:
        pass

    if html_snippet:
        try:
            found_node = _find_node_by_html_snippet(soup, html_snippet)
            if found_node:
                found_node.replace_with(new_node)
                return True
        except Exception:
            pass

    try:
        found_node = _find_node_by_selector(soup, selector, html_snippet, 0)
        if found_node:
            found_node.replace_with(new_node)
            return True
    except Exception:
        pass

    return False