import json

# Leer el reporte de AXE
with open('results/ngx_admin/2025-12-04_13-52-28/angular_axe_report.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

violations = data.get('violations', [])
print(f'Total violations: {len(violations)}\n')

total_nodes = 0
for i, v in enumerate(violations, 1):
    nodes = v.get('nodes', [])
    total_nodes += len(nodes)
    print(f"{i}. {v.get('id', 'unknown')}")
    print(f"   Description: {v.get('description', '')}")
    print(f"   Impact: {v.get('impact', 'unknown')}")
    print(f"   Nodes affected: {len(nodes)}")
    if nodes:
        print(f"   First node selector: {nodes[0].get('target', ['N/A'])[0] if nodes[0].get('target') else 'N/A'}")
    print()

print(f"\nTotal nodes with violations: {total_nodes}")

