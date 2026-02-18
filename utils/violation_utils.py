"""
Utilities for processing accessibility violations.

This module provides functions to group, flatten and prioritise
accessibility violations reported by Axe.
"""

from typing import Any, Dict, List

# Default values
_DEFAULT_VIOLATION_ID = 'unknown-violation'
_DEFAULT_DESCRIPTION = 'No description'
_DEFAULT_IMPACT = 'moderate'
_DEFAULT_SELECTOR = 'No selector'
_DEFAULT_HTML_SNIPPET = 'No HTML snippet'

# Impact priority order
_IMPACT_PRIORITY: Dict[str, int] = {
    'critical': 1,
    'serious': 2,
    'moderate': 3,
    'minor': 4
}


def group_and_simplify_violations(violations: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Group violations by type and improve information for consistent processing.

    Args:
        violations: List of Axe violations

    Returns:
        Dictionary with violations grouped by ID
    """
    grouped_violations: Dict[str, Dict[str, Any]] = {}
    if not violations:
        return grouped_violations
        
    for violation in violations:
        violation_id = violation.get('id', _DEFAULT_VIOLATION_ID)
        description = violation.get('help', _DEFAULT_DESCRIPTION)
        impact = violation.get('impact', _DEFAULT_IMPACT)
        
        if violation_id not in grouped_violations:
            grouped_violations[violation_id] = {
                "description": description,
                "impact": impact,
                "nodes": [],
                "total_count": 0
            }

        for node in violation.get('nodes', []):
            selector = node.get('target', [_DEFAULT_SELECTOR])[0]
            html_snippet = node.get('html', _DEFAULT_HTML_SNIPPET)
            failure_summary = node.get('failureSummary', '')
            
            node_info = {
                "selector": selector,
                "html": html_snippet,
                "failure_summary": failure_summary,
                "element_info": f"Element: <{selector}>, Code: `{html_snippet}`"
            }
            grouped_violations[violation_id]["nodes"].append(node_info)
            grouped_violations[violation_id]["total_count"] += 1
            
    return grouped_violations

def flatten_violations(violations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert grouped violations into a flat list with improved information.

    Args:
        violations: List of Axe violations

    Returns:
        Flat list of violations with expanded information
    """
    flat_list: List[Dict[str, Any]] = []
    if not violations:
        return flat_list
        
    for violation in violations:
        violation_id = violation.get('id', _DEFAULT_VIOLATION_ID)
        description = f"Error Type: '{violation_id}' - {violation.get('help')}"
        impact = violation.get('impact', _DEFAULT_IMPACT)
        
        for node in violation.get('nodes', []):
            selector = node.get('target', [None])[0]
            if not selector:
                continue
                
            violation_data = {
                "description": description,
                "selector": selector,
                "violation_id": violation_id,
                "impact": impact,
                "html_snippet": node.get('html', ''),
                "failure_summary": node.get('failureSummary', '')
            }
            
            if violation_id == 'color-contrast':
                _extract_contrast_data(node, violation_data)
            
            flat_list.append(violation_data)
    
    return flat_list


def _extract_contrast_data(node: Dict[str, Any], violation_data: Dict[str, Any]) -> None:
    """Extract colour contrast-specific data from the node."""
    any_data = node.get('any', [])
    if not any_data:
        return
        
    contrast_data = any_data[0].get('data', {})
    if contrast_data:
        violation_data['contrast_data'] = {
            'bgColor': contrast_data.get('bgColor'),
            'fgColor': contrast_data.get('fgColor'),
            'contrastRatio': contrast_data.get('contrastRatio'),
            'expectedContrastRatio': contrast_data.get('expectedContrastRatio'),
            'fontSize': contrast_data.get('fontSize'),
            'fontWeight': contrast_data.get('fontWeight')
        }

def prioritize_violations(violations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Prioritise violations by impact and type for more effective processing.

    Args:
        violations: List of violations to prioritise

    Returns:
        List of violations ordered by priority
    """
    return sorted(
        violations,
        key=lambda v: (
            _IMPACT_PRIORITY.get(v.get('impact', _DEFAULT_IMPACT), 3),
            v.get('id', _DEFAULT_VIOLATION_ID)
        )
    )