def group_and_simplify_violations(violations):
    grouped_violations = {}
    if not violations:
        return grouped_violations
        
    for violation in violations:
        violation_id = violation.get('id', 'unknown-violation')
        description = violation.get('help', 'No description')
        
        if violation_id not in grouped_violations:
            grouped_violations[violation_id] = {
                "description": description,
                "nodes": []
            }

        for node in violation.get('nodes', []):
            selector = node.get('target', ['No selector'])[0]
            html_snippet = node.get('html', 'No HTML snippet')
            
            # Crear una cadena de texto concisa para cada nodo afectado
            error_string = f"Element: <{selector}>, Code: `{html_snippet}`"
            grouped_violations[violation_id]["nodes"].append(error_string)
            
    return grouped_violations

def flatten_violations(violations):
    flat_list = []
    if not violations:
        return flat_list
        
    for violation in violations:
        description = f"Error Type: '{violation.get('id')}' - {violation.get('help')}"
        for node in violation.get('nodes', []):
            # Usamos el primer selector de la lista, que suele ser el más específico
            selector = node.get('target', [None])[0]
            if selector:
                flat_list.append({
                    "description": description,
                    "selector": selector,
                })
    return flat_list