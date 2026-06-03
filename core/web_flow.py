"""
Web URL orchestration flow.

This module encapsulates the end-to-end flow for analyzing and repairing a
public URL, including local preview serving for the generated HTML result.
"""

import http.server
import json
import os
import socketserver
import webbrowser
from datetime import datetime
from typing import Callable
from urllib.parse import urlparse

from core.analyzer import run_axe_analysis, run_axe_analysis_with_driver
from core.html_generator import generate_accessible_html_with_parser
from core.report import generate_comparison_report
from core.webdriver_setup import setup_driver
from utils.io_utils import clear_openai_logs, save_openai_logs, setup_directories

DEFAULT_SERVER_PORT = 8000
MAX_SERVER_PORT = 8050
PREVIEW_PROMPT = "\nDo you want to preview the corrected page in your browser? (y/n): "



def execute_web_url_flow(args, client, timestamp: str, create_run_path: Callable[[str, str], str]) -> None:
    """Execute the full web-page accessibility workflow for a public URL."""
    start_time = datetime.now()
    run_path = create_run_path(urlparse(args.url).netloc, timestamp)

    setup_directories(run_path)
    clear_openai_logs()

    driver = None
    accessible_page_path = None

    try:
        driver = setup_driver()

        if args.analyze_only:
            initial_results, driver = run_axe_analysis_with_driver(
                driver,
                args.url,
                enable_dynamic_interactions=not args.disable_dynamic,
            )
            results_path = os.path.join(run_path, "axe_results.json")
            with open(results_path, "w", encoding="utf-8") as file:
                json.dump(initial_results, file, ensure_ascii=False, indent=2)
            print(f"Analysis completed. Results saved to: {results_path}")

            from utils.color_catalog_utils import extract_color_catalog

            extract_color_catalog(run_path, run_path)
            return

        if args.fix_only:
            if not args.analysis_results or not os.path.exists(args.analysis_results):
                print("You must provide --analysis-results with the path to the analysis JSON file.")
                return
            with open(args.analysis_results, "r", encoding="utf-8") as file:
                initial_results = json.load(file)
            if not initial_results or not initial_results.get("violations"):
                print("No violations found in analysis results.")
                return

            driver.get(args.url)
            original_html = driver.page_source
            accessible_html = generate_accessible_html_with_parser(
                original_html,
                initial_results,
                [],
                client,
                args.url,
                driver,
                [],
            )
            accessible_page_path = os.path.join(run_path, "accessible_page.html")
            with open(accessible_page_path, "w", encoding="utf-8") as file:
                file.write(accessible_html)
            print(f"Fix completed. Accessible HTML saved to: {accessible_page_path}")
            return

        initial_results, driver = run_axe_analysis_with_driver(
            driver,
            args.url,
            enable_dynamic_interactions=not args.disable_dynamic,
        )

        if not initial_results or not initial_results.get("violations"):
            print("No violations found.")
            return

        original_html = driver.page_source
        accessible_html = generate_accessible_html_with_parser(
            original_html,
            initial_results,
            [],
            client,
            args.url,
            driver,
            [],
        )

        accessible_page_path = os.path.join(run_path, "accessible_page.html")
        with open(accessible_page_path, "w", encoding="utf-8") as file:
            file.write(accessible_html)

        final_results, driver = run_axe_analysis_with_driver(
            driver,
            accessible_page_path,
            is_local_file=True,
        )
        report_path = os.path.join(run_path, "comparison_report.html")
        elapsed_seconds = (datetime.now() - start_time).total_seconds()

        generate_comparison_report(
            initial_results,
            final_results,
            report_path,
            elapsed_seconds,
        )
        save_openai_logs(run_path)
    except Exception as exc:
        print(f"Unexpected error: {exc}")
    finally:
        if driver:
            driver.quit()

    if accessible_page_path and os.path.exists(accessible_page_path):
        _serve_preview_if_requested(accessible_page_path)



def _find_available_port():
    """Find an available port within the configured range."""
    handler = http.server.SimpleHTTPRequestHandler

    for port in range(DEFAULT_SERVER_PORT, MAX_SERVER_PORT):
        try:
            return socketserver.TCPServer(("", port), handler)
        except OSError:
            continue

    return None



def _serve_preview_if_requested(accessible_page_path: str) -> None:
    """Optionally launch a local preview server for the accessible HTML output."""
    if input(PREVIEW_PROMPT).lower() != "y":
        return

    abs_path = os.path.abspath(accessible_page_path)
    base_dir = os.path.dirname(abs_path)
    file_name = os.path.basename(abs_path)

    os.chdir(base_dir)
    httpd = _find_available_port()

    if not httpd:
        print("Could not start server.")
        return

    url_to_open = f"http://localhost:{httpd.server_address[1]}/{file_name}"
    print(f"Server started at: {url_to_open}")
    webbrowser.open_new_tab(url_to_open)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
