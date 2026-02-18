import argparse
import http.server
import json
import os
import socketserver
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI

from config.constants import BASE_RESULTS_DIR

from core.analyzer import (
    run_axe_analysis,
    run_axe_analysis_multiple_states,
)

from core.angular_handler import process_angular_project
from core.react_handler import (
    detect_react_project,
    run_axe_on_react_app,
    map_axe_violations_to_react_components,
    fix_react_components_with_axe_violations,
)

from core.html_generator import generate_accessible_html_with_parser
from core.image_processing import process_media_elements

from core.report import generate_comparison_report
from core.screenshot_handler import take_screenshots

from core.webdriver_setup import setup_driver
from core.ports import detect_react_dev_server_port as _detect_react_dev_server_port

from utils.io_utils import clear_openai_logs, save_openai_logs, setup_directories

load_dotenv()


# Constants
DEFAULT_SERVER_PORT = 8000
MAX_SERVER_PORT = 8050

PREVIEW_PROMPT = "\nDo you want to preview the corrected page in your browser? (y/n): "

# Argument parsing
def _create_argument_parser() -> argparse.ArgumentParser:
    """
    Create and configure the CLI argument parser.

    Returns:
        argparse.ArgumentParser: Configured argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Accessibility analyzer and enhancer with AI."
    )

    # Execution modes
    parser.add_argument("--url", type=str, help="URL of the page to analyze.")
    parser.add_argument(
        "--project-path",
        type=str,
        help="Path to a local project (Angular or React)."
    )

    # OpenAI configuration
    parser.add_argument("--api-key", type=str, default=None)

    # Web interaction configuration
    parser.add_argument(
        "--disable-dynamic",
        action="store_true",
        help="Disable automatic dynamic interactions."
    )
    parser.add_argument("--interactions-file", type=str)
    parser.add_argument("--multi-state-file", type=str)

    # Angular flow options
    parser.add_argument("--serve-app", action="store_true")
    parser.add_argument("--angular-axe", action="store_true")
    parser.add_argument("--angular-axe-only", action="store_true")
    parser.add_argument(
        "--angular-url",
        type=str,
        default="http://localhost:4200/"
    )

    # React flow options
    parser.add_argument("--react-axe", action="store_true")
    parser.add_argument("--react-axe-only", action="store_true")
    parser.add_argument(
        "--react-url",
        type=str,
        default="http://localhost:3000/"
    )

    return parser


def _validate_arguments(args, parser: argparse.ArgumentParser) -> None:
    """
    Validate mutually exclusive execution modes.
    """
    if not args.url and not args.project_path:
        parser.error("You must provide --url or --project-path.")

    if args.url and args.project_path:
        parser.error("You must provide only one of the following modes: --url or --project-path.")

# API Key Handling
def _get_api_key(args) -> str:
    """
    Retrieve OpenAI API key from CLI argument or environment variable.
    """
    api_key = args.api_key or os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("The OpenAI API key has not been provided.")

    return api_key

# Utility Helpers
def _sanitize_name(name: str) -> str:
    """
    Convert a string into a filesystem-safe directory name.
    """
    return "".join(char if char.isalnum() else "_" for char in name)


def _create_run_path(base_name: str, timestamp: str) -> str:
    """
    Build the output directory path for the current execution.
    """
    sanitized_name = _sanitize_name(base_name)
    return os.path.join(BASE_RESULTS_DIR, sanitized_name, timestamp)


def _load_json_file(file_path: Optional[str], error_prefix: str):
    """
    Load a JSON file safely.

    Returns:
        dict | None
    """
    if not file_path:
        return None

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        print(f"{error_prefix}: {exc}")
        return None

# Main Function
def main() -> None:
    """
    Main entry point of the application.
    """
    parser = _create_argument_parser()
    args = parser.parse_args()
    _validate_arguments(args, parser)

    try:
        api_key = _get_api_key(args)
    except ValueError as exc:
        print(f"Error: {exc}")
        return

    client = OpenAI(api_key=api_key)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Local project flow
    if args.project_path:
        _handle_local_project(args, client, timestamp)
        return

    # Web URL flow
    _process_web_url(args, client, timestamp)

# Local Project Router
def _handle_local_project(args, client, timestamp: str) -> None:
    """
    Decide whether to execute Angular or React flow for a local project.
    """
    project_path = os.path.abspath(args.project_path)

    is_react = (
        detect_react_project(project_path)
        or args.react_axe
        or args.react_axe_only
    )

    force_angular = args.angular_axe or args.angular_axe_only

    if force_angular:
        print(f"[Detection] Project treated as Angular: {project_path}")
        _process_angular_project(args, client, timestamp)
    elif is_react:
        print(f"[Detection] React project detected: {project_path}")
        _process_react_project_flow(args, client, timestamp)
    else:
        _process_angular_project(args, client, timestamp)


# React Flow
def _process_react_project_flow(args, client, timestamp: str) -> None:
    """
    Execute advanced React + Axe flow.
    """
    project_path = os.path.abspath(args.project_path)
    project_name = os.path.basename(project_path.rstrip(os.sep)) or "react_project"

    run_path = _create_run_path(project_name, timestamp)

    setup_directories(run_path)
    clear_openai_logs()

    react_axe_enabled = args.react_axe or args.react_axe_only

    if react_axe_enabled:

        detected_port = _detect_react_dev_server_port(project_path)

        if detected_port:
            react_url = f"http://localhost:{detected_port}/"
        else:
            react_url = args.react_url

        print(f"[React + Axe] Executing analysis on: {react_url}")

        try:
            axe_results, screenshot_paths = run_axe_on_react_app(
                react_url,
                run_path,
                suffix="_before",
                take_screenshots_flag=True
            )

            issues_by_component = map_axe_violations_to_react_components(
                axe_results,
                Path(project_path)
            )

            if issues_by_component:
                fixes = fix_react_components_with_axe_violations(
                    issues_by_component,
                    Path(project_path),
                    client,
                    screenshot_paths=screenshot_paths
                )
                print(f"[React + Axe] Components fixed: {len(fixes)}")    
            else:
                print("[React + Axe] No violations mapped to components.")

        except Exception as exc:
            print(f"[React + Axe] Error: {exc}")

    save_openai_logs(run_path)
    print("React process completed.")


# Angular Flow
def _process_angular_project(args, client, timestamp: str) -> None:
    """
    Process Angular project (classic + advanced Axe flow).
    """
    project_path = os.path.abspath(args.project_path)
    project_name = os.path.basename(project_path.rstrip(os.sep)) or "angular_project"

    run_path = _create_run_path(project_name, timestamp)
    start_time = datetime.now()

    setup_directories(run_path)
    clear_openai_logs()

    try:
        summary = process_angular_project(
            project_path,
            client,
            run_path,
            serve_app=args.serve_app
        )
        if summary:
            print("\n--- Angular summary ---")
            for line in summary:
                print(line)
    except Exception as exc:
        print(f"Error processing Angular project: {exc}")

    save_openai_logs(run_path)

    elapsed = int((datetime.now() - start_time).total_seconds())
    print(f"Total execution time Angular: {elapsed}s")


# Web URL Flow
def _process_web_url(args, client, timestamp: str) -> None:
    """
    Execute full web page accessibility correction workflow.
    """
    start_time = datetime.now()

    sanitized_url = _sanitize_name(urlparse(args.url).netloc)
    run_path = _create_run_path(sanitized_url, timestamp)

    setup_directories(run_path)
    clear_openai_logs()

    driver = None
    accessible_page_path = None

    try:
        driver = setup_driver()

        initial_results = run_axe_analysis(
            driver,
            args.url,
            enable_dynamic_interactions=not args.disable_dynamic
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
            []
        )

        accessible_page_path = os.path.join(run_path, "accessible_page.html")

        with open(accessible_page_path, "w", encoding="utf-8") as file:
            file.write(accessible_html)

        final_results = run_axe_analysis(
            driver,
            accessible_page_path,
            is_local_file=True
        )

        report_path = os.path.join(run_path, "comparison_report.html")

        elapsed_seconds = (datetime.now() - start_time).total_seconds()

        generate_comparison_report(
            initial_results,
            final_results,
            report_path,
            elapsed_seconds
        )

        save_openai_logs(run_path)

    except Exception as exc:
        print(f"Unexpected error: {exc}")

    finally:
        if driver:
            driver.quit()

    if accessible_page_path and os.path.exists(accessible_page_path):
        _serve_preview_if_requested(accessible_page_path)

# Preview Server
def _find_available_port():
    """Find available port within configured range."""
    Handler = http.server.SimpleHTTPRequestHandler

    for port in range(DEFAULT_SERVER_PORT, MAX_SERVER_PORT):
        try:
            return socketserver.TCPServer(("", port), Handler)
        except OSError:
            continue

    return None

def _serve_preview_if_requested(accessible_page_path: str) -> None:
    """
    Optionally launch local preview server.
    """
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



if __name__ == "__main__":
    main()