"""
Utilidades para procesamiento de violaciones de accesibilidad.

Este módulo proporciona funciones para agrupar, aplanar y priorizar
violaciones de accesibilidad detectadas por Axe.
"""

# Valores por defecto
_DEFAULT_VIOLATION_ID = 'unknown-violation'
_DEFAULT_DESCRIPTION = 'No description'
_DEFAULT_IMPACT = 'moderate'
_DEFAULT_SELECTOR = 'No selector'
_DEFAULT_HTML_SNIPPET = 'No HTML snippet'

# Orden de prioridad de impactos
_IMPACT_PRIORITY = {
    'critical': 1,
    'serious': 2,
    'moderate': 3,
    'minor': 4
}


def group_and_simplify_violations(violations):
    """
    Agrupa violaciones por tipo y mejora la información para procesamiento consistente.
    
    Args:
        violations: Lista de violaciones de Axe
        
    Returns:
        Diccionario con violaciones agrupadas por ID
    """
    grouped_violations = {}
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

def flatten_violations(violations):
    """
    Convierte violaciones agrupadas en lista plana con información mejorada.
    
    Args:
        violations: Lista de violaciones de Axe
        
    Returns:
        Lista plana de violaciones con información expandida
    """
    flat_list = []
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


def _extract_contrast_data(node, violation_data):
    """Extrae datos específicos de contraste de color del nodo."""
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

def prioritize_violations(violations):
    """
    Prioriza violaciones por impacto y tipo para procesamiento más efectivo.
    
    Args:
        violations: Lista de violaciones a priorizar
        
    Returns:
        Lista de violaciones ordenadas por prioridad
    """
    return sorted(
        violations,
        key=lambda v: (
            _IMPACT_PRIORITY.get(v.get('impact', _DEFAULT_IMPACT), 3),
            v.get('id', _DEFAULT_VIOLATION_ID)
        )
    )