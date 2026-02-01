import json

# Leer el reporte de AXE
with open('results/ngx_admin/2025-12-05_08-51-44/angular_axe_report.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

violations = data.get('violations', [])

print("=" * 80)
print("DETALLES DE VIOLACIONES DE ACCESIBILIDAD (AXE)")
print("=" * 80)

for i, v in enumerate(violations, 1):
    nodes = v.get('nodes', [])
    print(f"\n{i}. {v.get('id', 'unknown').upper()}")
    print(f"   Descripción: {v.get('description', '')}")
    print(f"   Impacto: {v.get('impact', 'unknown')}")
    print(f"   Ayuda: {v.get('help', '')}")
    print(f"   Nodos afectados: {len(nodes)}")
    print(f"   URL ayuda: {v.get('helpUrl', '')}")
    
    # Mostrar primeros 3 nodos como ejemplo
    for j, node in enumerate(nodes[:3], 1):
        target = node.get('target', [])
        html = node.get('html', '')[:150]
        print(f"\n   Nodo {j}:")
        print(f"      Selector: {target[0] if target else 'N/A'}")
        print(f"      HTML: {html}...")
        if 'failureSummary' in node:
            print(f"      Resumen: {node['failureSummary'][:200]}...")
    
    if len(nodes) > 3:
        print(f"   ... y {len(nodes) - 3} nodos más")

print("\n" + "=" * 80)
print(f"TOTAL: {len(violations)} tipos de violaciones, {sum(len(v.get('nodes', [])) for v in violations)} nodos afectados")
print("=" * 80)

