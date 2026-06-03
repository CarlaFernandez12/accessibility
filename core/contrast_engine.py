"""Deterministic helpers for identifying and repairing contrast issues."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bs4 import Tag

from core.html_contrast import find_contrasting_color


def is_contrast_violation(violation: Dict[str, Any]) -> bool:
    """Return True when the violation represents a contrast issue."""
    violation_id = (
        violation.get("violation_id")
        or violation.get("id")
        or violation.get("type")
        or violation.get("issueType")
        or violation.get("violation", {}).get("id")
        or ""
    )
    normalized_id = str(violation_id).lower()
    return normalized_id in {"color-contrast", "contrast"}


def split_contrast_violations(
    violations: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a collection into contrast and non-contrast violations."""
    contrast: List[Dict[str, Any]] = []
    non_contrast: List[Dict[str, Any]] = []
    for violation in violations:
        if is_contrast_violation(violation):
            contrast.append(violation)
        else:
            non_contrast.append(violation)
    return contrast, non_contrast


def extract_contrast_payload(violation: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize contrast metadata for deterministic repair/reporting."""
    contrast_data = violation.get("contrast_data", {}) or {}
    required_raw = contrast_data.get("expectedContrastRatio") or "4.5:1"
    required_ratio = _parse_required_ratio(required_raw)
    background = contrast_data.get("bgColor") or ""
    foreground = contrast_data.get("fgColor") or ""
    suggested_color = find_contrasting_color(background, required_ratio) if background else "#000000"

    return {
        "issueId": violation.get("issueId") or violation.get("issue_id") or violation.get("selector") or "contrast",
        "type": "contrast",
        "foreground": foreground,
        "background": background,
        "ratio": contrast_data.get("contrastRatio"),
        "required": required_ratio,
        "suggestedColor": suggested_color,
        "source": violation.get("source", {}),
    }


def load_color_catalog(catalog_path: str) -> List[Dict[str, Any]]:
    """Load a color catalog JSON file when it exists."""
    if not catalog_path:
        return []

    path = Path(catalog_path)
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def extract_contrast_data_from_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """Extract Axe contrast metadata from a node payload."""
    for bucket in ("all", "any"):
        for check in node.get(bucket, []) or []:
            check_data = check.get("data") or {}
            if check_data.get("fgColor") or check_data.get("bgColor") or check_data.get("contrastRatio"):
                return {
                    "fgColor": check_data.get("fgColor") or "",
                    "bgColor": check_data.get("bgColor") or "",
                    "contrastRatio": check_data.get("contrastRatio"),
                    "expectedContrastRatio": check_data.get("expectedContrastRatio") or "4.5:1",
                    "fontSize": check_data.get("fontSize") or "",
                    "fontWeight": check_data.get("fontWeight") or "",
                }
    return {}


def build_node_contrast_issue(violation: Dict[str, Any], node: Dict[str, Any]) -> Dict[str, Any]:
    """Build a normalized contrast issue for a single Axe node."""
    targets = node.get("target") or []
    return {
        "violation": violation,
        "violation_id": violation.get("id", "color-contrast"),
        "node": node,
        "selector": targets[0] if targets else None,
        "contrast_data": extract_contrast_data_from_node(node),
    }


def apply_source_catalog_contrast_repair(
    issue: Dict[str, Any],
    color_catalog: List[Dict[str, Any]],
    project_root: str,
) -> Optional[Dict[str, Any]]:
    """Repair a contrast issue by editing the most likely source token from the color catalog."""
    payload = extract_contrast_payload(issue)
    entry = _find_best_catalog_entry(issue, color_catalog, project_root)
    if not entry:
        return None

    file_path = Path(str(entry.get("file") or ""))
    if not file_path.exists():
        return None

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    replacement_value = _build_replacement_value(entry, payload["suggestedColor"])
    updated_content = _replace_catalog_entry_value(content, entry, replacement_value)
    if updated_content is None or updated_content == content:
        return None

    file_path.write_text(updated_content, encoding="utf-8")
    payload.update(
        {
            "status": "repaired-at-source",
            "sourceFile": str(file_path),
            "sourceLine": entry.get("line"),
            "sourceColumn": entry.get("column"),
        }
    )
    return payload


def apply_manual_contrast_fix(
    node: Tag,
    violation: Dict[str, Any],
    include_text_children: bool = True,
) -> Optional[Dict[str, Any]]:
    """Apply a deterministic text-color repair to the matched DOM node."""
    if node is None:
        return None

    payload = extract_contrast_payload(violation)
    color = payload["suggestedColor"]

    updated = _set_text_color(node, color)
    if include_text_children:
        for child in node.find_all(True):
            if child.get_text(strip=True):
                updated = _set_text_color(child, color) or updated

    if not updated:
        return None

    payload["status"] = "repaired-manually"
    return payload


def apply_manual_markup_contrast_repairs(
    markup: str,
    issues: Iterable[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Apply deterministic contrast repairs to HTML-like source markup."""
    updated_markup = markup
    repaired: List[Dict[str, Any]] = []

    for issue in issues:
        if not is_contrast_violation(issue):
            continue

        payload = extract_contrast_payload(issue)
        color = payload["suggestedColor"]
        changed = False

        for matcher in (
            _replace_by_html_snippet,
            _replace_by_selector_hint,
            _replace_by_text_hint,
        ):
            updated_markup, changed = matcher(updated_markup, issue, color)
            if changed:
                payload["status"] = "repaired-manually"
                repaired.append(payload)
                break

    return updated_markup, repaired


def _set_text_color(node: Tag, color: str) -> bool:
    style = node.get("style", "")
    new_style = _upsert_color_declaration(style, color)
    if new_style == style:
        return False
    node["style"] = new_style
    return True


def _upsert_color_declaration(style: str, color: str) -> str:
    clean_style = (style or "").strip()
    color_decl = f"color: {color} !important"

    if not clean_style:
        return color_decl

    if re.search(r"(^|;)\s*color\s*:", clean_style, re.IGNORECASE):
        updated = re.sub(
            r"(^|;)\s*color\s*:\s*[^;]+",
            lambda match: f"{match.group(1)} {color_decl}".rstrip(),
            clean_style,
            count=1,
            flags=re.IGNORECASE,
        )
        return _normalize_style(updated)

    return _normalize_style(f"{clean_style}; {color_decl}")


def _normalize_style(style: str) -> str:
    parts = [segment.strip() for segment in style.split(";") if segment.strip()]
    return "; ".join(parts)


def _replace_by_html_snippet(markup: str, issue: Dict[str, Any], color: str) -> Tuple[str, bool]:
    html_snippet = issue.get("node", {}).get("html") or issue.get("html_snippet") or ""
    normalized_snippet = re.sub(r'\s+(_ngcontent|_nghost|ng-reflect)-[^= ]*="[^"]*"', "", html_snippet)
    opening_tag_match = re.search(r"<([a-zA-Z0-9_-]+)([^>]*)>", normalized_snippet)
    if not opening_tag_match:
        return markup, False

    tag_name = opening_tag_match.group(1)
    attributes = opening_tag_match.group(2)
    attributes = re.sub(r'\s+(_ngcontent|_nghost|ng-reflect)-[^= ]*="[^"]*"', "", attributes)
    selector = _extract_selector_from_attributes(tag_name, attributes)
    if not selector:
        return markup, False
    return _replace_first_opening_tag(markup, selector, color)


def _replace_by_selector_hint(markup: str, issue: Dict[str, Any], color: str) -> Tuple[str, bool]:
    targets = issue.get("node", {}).get("target") or [issue.get("selector")]
    selector = targets[0] if targets else ""
    if not selector:
        return markup, False

    class_matches = re.findall(r"\.([a-zA-Z0-9_-]+)", selector)
    if class_matches:
        class_pattern = "".join(
            rf"(?=[^>]*\b{re.escape(css_class)}\b)"
            for css_class in class_matches[:2]
        )
        tag_pattern = rf"<([a-zA-Z0-9_-]+){class_pattern}[^>]*>"
        return _replace_first_opening_tag(markup, tag_pattern, color, regex_is_complete=True)

    return markup, False


def _replace_by_text_hint(markup: str, issue: Dict[str, Any], color: str) -> Tuple[str, bool]:
    html_snippet = issue.get("node", {}).get("html") or issue.get("html_snippet") or ""
    tag_match = re.search(r"<([a-zA-Z0-9_-]+)", html_snippet)
    text = re.sub(r"<[^>]+>", " ", html_snippet)
    text = re.sub(r"\s+", " ", text).strip()
    if not tag_match or len(text) < 3:
        return markup, False

    tag_name = tag_match.group(1)
    block_pattern = rf"(<{tag_name}\b[^>]*>)(?:(?!</{tag_name}>).)*?{re.escape(text)}(?:(?!</{tag_name}>).)*?(</{tag_name}>)"
    match = re.search(block_pattern, markup, re.IGNORECASE | re.DOTALL)
    if not match:
        return markup, False

    opening_tag = match.group(1)
    updated_opening_tag = _inject_html_style(opening_tag, color)
    if updated_opening_tag == opening_tag:
        return markup, False
    return markup.replace(opening_tag, updated_opening_tag, 1), True


def _extract_selector_from_attributes(tag_name: str, attributes: str) -> str:
    id_match = re.search(r'\sid="([^"]+)"', attributes)
    if id_match:
        return rf"<{tag_name}\b(?=[^>]*\sid=\"{re.escape(id_match.group(1))}\")[^>]*>"

    class_match = re.search(r'\sclass="([^"]+)"', attributes)
    if class_match:
        classes = [value for value in class_match.group(1).split() if value]
        if classes:
            class_pattern = "".join(f"(?=[^>]*\\b{re.escape(css_class)}\\b)" for css_class in classes[:2])
            return rf"<{tag_name}\b{class_pattern}[^>]*>"

    type_match = re.search(r'\stype="([^"]+)"', attributes)
    if type_match:
        return rf"<{tag_name}\b(?=[^>]*\stype=\"{re.escape(type_match.group(1))}\")[^>]*>"

    return rf"<{tag_name}\b[^>]*>"


def _replace_first_opening_tag(markup: str, selector: str, color: str, regex_is_complete: bool = False) -> Tuple[str, bool]:
    pattern = selector if regex_is_complete else selector
    match = re.search(pattern, markup, re.IGNORECASE | re.DOTALL)
    if not match:
        return markup, False

    opening_tag = match.group(0)
    updated_opening_tag = _inject_html_style(opening_tag, color)
    if updated_opening_tag == opening_tag:
        return markup, False

    return markup.replace(opening_tag, updated_opening_tag, 1), True


def _inject_html_style(opening_tag: str, color: str) -> str:
    style_match = re.search(r'style="([^"]*)"', opening_tag, re.IGNORECASE)
    if style_match:
        updated_style = _upsert_color_declaration(style_match.group(1), color)
        return opening_tag.replace(style_match.group(0), f'style="{updated_style}"', 1)
    return opening_tag[:-1] + f' style="color: {color} !important">'


def _parse_required_ratio(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "4.5:1")
    try:
        return float(text.replace(":1", ""))
    except ValueError:
        return 4.5


def _find_best_catalog_entry(
    issue: Dict[str, Any],
    color_catalog: List[Dict[str, Any]],
    project_root: str,
) -> Optional[Dict[str, Any]]:
    foreground = _normalize_color_token(issue.get("contrast_data", {}).get("fgColor") or "")
    if not foreground:
        return None

    issue_selector = str((issue.get("node", {}).get("target") or [issue.get("selector")])[0] or "")
    issue_html = str(issue.get("node", {}).get("html") or issue.get("html_snippet") or "")
    selector_classes = re.findall(r"\.([a-zA-Z0-9_-]+)", issue_selector)
    html_classes = re.findall(r'class="([^"]+)"', issue_html)
    html_class_tokens = [token for value in html_classes for token in value.split() if token]
    tag_match = re.search(r"<([a-zA-Z0-9_-]+)", issue_html)
    tag_name = tag_match.group(1).lower() if tag_match else ""

    candidates: List[Tuple[int, Dict[str, Any]]] = []
    for entry in color_catalog:
        file_path = str(entry.get("file") or "")
        if not file_path.startswith(project_root):
            continue

        entry_color = _normalize_color_token(entry.get("resolved") or entry.get("value") or "")
        if entry_color != foreground:
            continue

        entry_type = str(entry.get("type") or "")
        if entry_type not in {"color", "inline-style", "raw-color", "css-variable"}:
            continue

        score = {"color": 20, "inline-style": 18, "raw-color": 12, "css-variable": 8}.get(entry_type, 0)
        selector_context = _extract_selector_context(Path(file_path), int(entry.get("line") or 1)).lower()
        line_text = _extract_line_text(Path(file_path), int(entry.get("line") or 1)).lower()

        for css_class in selector_classes + html_class_tokens:
            if css_class and (css_class.lower() in selector_context or css_class.lower() in line_text):
                score += 6

        if tag_name and (f"{tag_name}" in selector_context or f"<{tag_name}" in line_text):
            score += 2

        variable_name = str(entry.get("variable") or "")
        if variable_name and variable_name.lower() in line_text:
            score += 3

        if "routerlink" in issue_html.lower() and "routerlink" in line_text:
            score += 2

        candidates.append((score, entry))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_entry = candidates[0]
    return best_entry if best_score >= 8 else None


def _normalize_color_token(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""

    text = text.replace("!important", "").strip()
    if re.fullmatch(r"#[0-9a-f]{3}", text):
        return "#" + "".join(ch * 2 for ch in text[1:])
    return text


def _extract_selector_context(file_path: Path, line_number: int) -> str:
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""

    if file_path.suffix not in {".css", ".scss", ".sass"}:
        index = max(0, min(len(lines) - 1, line_number - 1)) if lines else 0
        return lines[index] if lines else ""

    collected: List[str] = []
    index = max(0, min(len(lines) - 1, line_number - 1))
    while index >= 0:
        line = lines[index].strip()
        if not line:
            index -= 1
            continue
        collected.insert(0, line)
        if "{" in line:
            break
        index -= 1
    return " ".join(collected)


def _extract_line_text(file_path: Path, line_number: int) -> str:
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    if not lines:
        return ""
    index = max(0, min(len(lines) - 1, line_number - 1))
    return lines[index]


def _build_replacement_value(entry: Dict[str, Any], suggested_color: str) -> str:
    original_value = str(entry.get("value") or "")
    suffix = " !important" if "!important" in original_value.lower() else ""
    return f"{suggested_color}{suffix}"


def _replace_catalog_entry_value(content: str, entry: Dict[str, Any], replacement_value: str) -> Optional[str]:
    lines = content.splitlines(keepends=True)
    line_number = int(entry.get("line") or 0)
    if line_number <= 0 or line_number > len(lines):
        return None

    line_index = line_number - 1
    line = lines[line_index]
    original_value = str(entry.get("value") or "")
    if not original_value:
        return None

    column = max(0, int(entry.get("column") or 1) - 1)
    search_window = line[column:]
    relative_index = search_window.lower().find(original_value.lower())
    if relative_index == -1:
        relative_index = line.lower().find(original_value.lower())
        if relative_index == -1:
            return None
        start_index = relative_index
    else:
        start_index = column + relative_index

    end_index = start_index + len(original_value)
    lines[line_index] = line[:start_index] + replacement_value + line[end_index:]
    return "".join(lines)