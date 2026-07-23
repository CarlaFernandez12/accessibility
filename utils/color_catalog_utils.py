"""
Color extraction utilities for HTML, CSS, Angular, React.

This module provides functions to extract all foreground/background colors
from templates, CSS/SCSS, and inline styles, and to resolve CSS variables.
"""

import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List

COLOR_REGEX = r"#[0-9a-fA-F]{3,8}|rgba?\([^\)]*\)|hsla?\([^\)]*\)"
CSS_VAR_REGEX = r"var\((--[\w-]+)\)"
STYLE_PROP_REGEX = r"(color|background(-color)?|border(-color)?|fill|stroke)\s*:\s*([^;]+);?"
CSS_VARIABLE_DEF_REGEX = r"(--[\w-]+)\s*:\s*([^;]+);"

IGNORE_DIRS = {
    "node_modules",
    "dist",
    ".git",
    "coverage",
    "build",
    "out",
    "__pycache__",
}

CatalogEntry = Dict[str, Any]
ColorExtractor = Callable[[str, str], List[CatalogEntry]]


def _get_line_column(text: str, offset: int) -> Dict[str, int]:
    prefix = text[:offset]
    line = prefix.count("\n") + 1
    last_newline = prefix.rfind("\n")
    column = offset + 1 if last_newline == -1 else offset - last_newline
    return {"line": line, "column": column}


def _should_ignore_path(path: str) -> bool:
    parts = set(os.path.normpath(path).split(os.sep))
    return not parts.isdisjoint(IGNORE_DIRS)


def _iter_project_files(project_root: str, extensions: Iterable[str]) -> Iterable[str]:
    for root, _, files in os.walk(project_root):
        if _should_ignore_path(root):
            continue

        for filename in files:
            if not filename.endswith(tuple(extensions)):
                continue

            file_path = os.path.join(root, filename)
            if not _should_ignore_path(file_path):
                yield file_path


def _append_file_catalog(
    catalog: List[CatalogEntry],
    file_paths: Iterable[str],
    extractor: ColorExtractor,
) -> None:
    for file_path in file_paths:
        try:
            with open(file_path, encoding="utf-8") as file:
                file_content = file.read()
        except Exception:
            continue

        catalog.extend(extractor(file_content, file_path))


def _extract_style_properties(
    style_text: str,
    context: str,
    file_path: str,
    source_text: str | None = None,
    source_offset: int = 0,
) -> List[CatalogEntry]:
    colors: List[CatalogEntry] = []

    for prop_match in re.finditer(STYLE_PROP_REGEX, style_text):
        prop = prop_match.group(1)
        value = prop_match.group(4).strip()
        variable_refs = re.findall(CSS_VAR_REGEX, value)
        location = _get_line_column(source_text or style_text, source_offset + prop_match.start())
        colors.append(
            {
                "type": prop,
                "value": value,
                "variable": variable_refs[0] if variable_refs else None,
                "resolved": None,
                "context": context,
                "file": file_path,
                **location,
            }
        )

    for color_match in re.finditer(COLOR_REGEX, style_text):
        location = _get_line_column(source_text or style_text, source_offset + color_match.start())
        colors.append(
            {
                "type": "raw-color",
                "value": color_match.group(0),
                "variable": None,
                "resolved": color_match.group(0),
                "context": context,
                "file": file_path,
                **location,
            }
        )

    return colors


def extract_colors_from_css(css_text: str, file_path: str = "") -> List[Dict[str, Any]]:
    """
    Extract all color values and CSS variable definitions from a CSS string.
    Returns a list of dicts: {type, value, variable, resolved, context}
    """
    colors: List[CatalogEntry] = []
    # Find variable definitions: --var: value;
    for var_match in re.finditer(CSS_VARIABLE_DEF_REGEX, css_text):
        var_name = var_match.group(1)
        var_value = var_match.group(2).strip()
        location = _get_line_column(css_text, var_match.start())
        colors.append(
            {
                "type": "css-variable",
                "variable": var_name,
                "value": var_value,
                "resolved": var_value,
                "context": "css-var-def",
                "file": file_path,
                **location,
            }
        )

    colors.extend(_extract_style_properties(css_text, "css-prop", file_path, css_text, 0))

    for color in colors:
        if color["type"] == "raw-color":
            color["context"] = "css-raw"

    return colors


def extract_colors_from_html(html_text: str, file_path: str) -> List[Dict[str, Any]]:
    """
    Extract color values from inline style attributes in HTML.
    Returns a list of dicts: {type, value, variable, resolved, context, file, line}
    """
    colors: List[CatalogEntry] = []
    for m in re.finditer(r'style\s*=\s*"([^"]+)"', html_text):
        style = m.group(1)
        colors.extend(_extract_style_properties(style, "inline-style", file_path, html_text, m.start(1)))
    return colors


def resolve_css_variables(colors: List[Dict[str, Any]]) -> None:
    """
    For each color with a variable reference, resolve its value if possible.
    Modifies the list in place.
    """
    var_map = {c["variable"]: c["value"] for c in colors if c["type"] == "css-variable"}
    for c in colors:
        if c["variable"] and c["resolved"] is None:
            c["resolved"] = var_map.get(c["variable"], None)


def _save_color_catalog(catalog: List[CatalogEntry], run_path: str) -> None:
    out_path = os.path.join(run_path, "color_catalog.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    print(f"[Color Catalog] Saved {len(catalog)} color entries to {out_path}")


def _extract_computed_colors_from_driver(driver, page_url: str) -> List[CatalogEntry]:
    """Extract unique rendered color values from the live DOM via getComputedStyle."""
    script = """
const props = [
  'color',
  'background-color',
  'border-top-color',
  'border-right-color',
  'border-bottom-color',
  'border-left-color',
  'fill',
  'stroke'
];
const out = [];
const seen = new Set();
const elements = Array.from(document.querySelectorAll('*'));
for (const el of elements) {
  const cs = window.getComputedStyle(el);
  for (const prop of props) {
    const value = (cs.getPropertyValue(prop) || '').trim();
    if (!value || value === 'transparent' || value === 'rgba(0, 0, 0, 0)') {
      continue;
    }
    const key = `${prop}|${value}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    out.push({
      type: prop,
      value,
      variable: null,
      resolved: value,
      context: 'computed-style',
      file: window.location.href || '',
      line: 0,
      column: 0
    });
  }
}
return out;
"""
    try:
        extracted = driver.execute_script(script)
    except Exception:
        return []

    if not isinstance(extracted, list):
        return []

    catalog: List[CatalogEntry] = []
    for item in extracted:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value", "")).strip()
        if not value:
            continue
        catalog.append(
            {
                "type": str(item.get("type", "computed-color")),
                "value": value,
                "variable": None,
                "resolved": value,
                "context": "computed-style",
                "file": str(item.get("file", page_url)),
                "line": 0,
                "column": 0,
            }
        )
    return catalog


def extract_color_catalog_from_web_page(driver, html_text: str, page_url: str, run_path: str) -> None:
    """Extract a color catalog for a public URL flow using HTML and rendered styles."""
    catalog: List[CatalogEntry] = []

    # Inline style attributes from page source.
    catalog.extend(extract_colors_from_html(html_text, page_url))

    # Embedded <style> blocks from page source.
    for style_match in re.finditer(r"<style[^>]*>(.*?)</style>", html_text, flags=re.IGNORECASE | re.DOTALL):
        catalog.extend(extract_colors_from_css(style_match.group(1), page_url))

    # Rendered/computed styles capture external stylesheets as applied in browser.
    catalog.extend(_extract_computed_colors_from_driver(driver, page_url))

    resolve_css_variables(catalog)

    # Deduplicate repeated entries while keeping first occurrence.
    unique_catalog: List[CatalogEntry] = []
    seen = set()
    for entry in catalog:
        key = (
            entry.get("type"),
            entry.get("value"),
            entry.get("resolved"),
            entry.get("context"),
            entry.get("file"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_catalog.append(entry)

    _save_color_catalog(unique_catalog, run_path)


def extract_color_catalog(project_root: str, run_path: str) -> None:
    """
    Extract all colors from HTML, CSS, SCSS in the project and save as color_catalog.json.
    """
    catalog: List[CatalogEntry] = []

    _append_file_catalog(
        catalog,
        _iter_project_files(project_root, (".css", ".scss")),
        extract_colors_from_css,
    )
    _append_file_catalog(
        catalog,
        _iter_project_files(project_root, (".html", ".jsx", ".tsx", ".component.html")),
        extract_colors_from_html,
    )

    resolve_css_variables(catalog)
    _save_color_catalog(catalog, run_path)
