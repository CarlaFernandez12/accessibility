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
    out_path = os.path.join(run_path, "color_catalog.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    print(f"[Color Catalog] Saved {len(catalog)} color entries to {out_path}")
