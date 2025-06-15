import json
from jinja2 import Environment, FileSystemLoader

def generate_comparison_report(initial_results, final_results, report_path):
    def count_violations(results, impact=None):
        count = 0
        for v in results.get('violations', []):
            if impact is None or v.get('impact') == impact:
                count += len(v.get('nodes', []))
        return count

    initial_total = count_violations(initial_results)
    final_total = count_violations(final_results)
    reduction = initial_total - final_total
    improvement_percent = (reduction / initial_total * 100) if initial_total>0 else 0

    template_str = get_html_template()

    env = Environment(loader=FileSystemLoader('.'))
    template = env.from_string(template_str)

    html_content = template.render(
        initial_total=initial_total,
        final_total=final_total,
        reduction=reduction,
        improvement_percent=improvement_percent,

    )

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"Informe comparativo generado en: {report_path}")


def get_html_template():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Informe de Mejora de Accesibilidad</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; background-color: #f8f9fa; color: #343a40; }
            .container { max-width: 900px; margin: 40px auto; padding: 30px; background-color: white; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
            h1, h2 { color: #212529; border-bottom: 2px solid #e9ecef; padding-bottom: 10px; }
            h1 { font-size: 2.5em; text-align: center; margin-bottom: 30px; }
            h2 { font-size: 1.8em; margin-top: 40px; }
            .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; text-align: center; margin-bottom: 40px; }
            .metric { background-color: #f1f3f5; padding: 20px; border-radius: 8px; }
            .metric .value { font-size: 2.5em; font-weight: bold; color: #007bff; }
            .metric .value.positive { color: #28a745; }
            .metric .value.negative { color: #dc3545; }
            .metric .label { font-size: 1em; color: #6c757d; }
            .details table { width: 100%; border-collapse: collapse; margin-top: 20px; }
            .details th, .details td { padding: 12px 15px; border: 1px solid #dee2e6; text-align: left; }
            .details th { background-color: #e9ecef; font-weight: 600; }
            .details td:nth-child(n+2) { text-align: center; }

        </style>
    </head>
    <body>
        <div class="container">
            <h1>Informe de Accesibilidad</h1>
            <h2>Resumen de la Mejora</h2>
            <div class="summary">
                <div class="metric"><div class="value">{{ initial_total }}</div><div class="label">Errores Iniciales</div></div>
                <div class="metric"><div class="value">{{ final_total }}</div><div class="label">Errores Finales</div></div>
                <div class="metric"><div class="value positive">+{{ reduction }}</div><div class="label">Errores Corregidos</div></div>
                <div class="metric"><div class="value positive">{{ "%.2f"|format(improvement_percent) }}%</div><div class="label">Mejora Relativa</div></div>
            </div>
            
        </div>
    </body>
    </html>
    """
    