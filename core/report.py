import json
from typing import Any, Dict, Optional

from jinja2 import Environment, FileSystemLoader


def generate_comparison_report(
    initial_results: Dict[str, Any],
    final_results: Dict[str, Any],
    report_path: str,
    elapsed_seconds: Optional[float] = None,
) -> None:
    """
    Render a high‑level HTML comparison report between initial and final Axe results.

    The report is intentionally simple and self‑contained, suitable for sharing
    as a static artifact in technical documentation or audits.
    """

    def count_violations(results: Dict[str, Any], impact: Optional[str] = None) -> int:
        """Count all violating nodes, optionally filtered by impact."""
        count = 0
        for violation in results.get("violations", []):
            if impact is None or violation.get("impact") == impact:
                count += len(violation.get("nodes", []))
        return count

    initial_total = count_violations(initial_results)
    final_total = count_violations(final_results)
    reduction = initial_total - final_total
    improvement_percent = (reduction / initial_total * 100) if initial_total > 0 else 0

    template_str = get_html_template()

    # Format elapsed time into a human‑readable string
    elapsed_time_str: Optional[str] = None
    if elapsed_seconds is not None:
        total_seconds = int(round(elapsed_seconds))
        minutes, seconds = divmod(total_seconds, 60)
        if minutes > 0:
            elapsed_time_str = f"{minutes} min {seconds} s"
        else:
            elapsed_time_str = f"{seconds} s"

    env = Environment(loader=FileSystemLoader("."))
    template = env.from_string(template_str)

    html_content = template.render(
        initial_total=initial_total,
        final_total=final_total,
        reduction=reduction,
        improvement_percent=improvement_percent,
        elapsed_time=elapsed_time_str,
    )

    with open(report_path, "w", encoding="utf-8") as file:
        file.write(html_content)

    print(f"Comparison report generated at: {report_path}")


def get_html_template() -> str:
    """
    Return the base HTML template used for the comparison report.

    The template is intentionally inlined to avoid additional template
    resolution complexity and keep the module self‑contained.
    """
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Accessibility Improvement Report</title>
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
            <h1>Accessibility Report</h1>
            <h2>Improvement Summary</h2>
            <div class="summary">
                <div class="metric"><div class="value">{{ initial_total }}</div><div class="label">Initial Errors</div></div>
                <div class="metric"><div class="value">{{ final_total }}</div><div class="label">Final Errors</div></div>
                <div class="metric"><div class="value positive">+{{ reduction }}</div><div class="label">Errors Fixed</div></div>
                <div class="metric"><div class="value positive">{{ "%.2f"|format(improvement_percent) }}%</div><div class="label">Relative Improvement</div></div>
                <div class="metric">
                    <div class="value {{ 'positive' if elapsed_time }}">{{ elapsed_time if elapsed_time else '-' }}</div>
                    <div class="label">Execution Time</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
