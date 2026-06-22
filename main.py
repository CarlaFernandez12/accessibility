import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI

from config.constants import BASE_RESULTS_DIR
from core.project_flow import execute_local_project_flow
from core.web_flow import execute_web_url_flow

load_dotenv()


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

    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Run analysis only and save the results as JSON."
    )
    parser.add_argument(
        "--fix-only",
        action="store_true",
        help="Run fixes only from a saved analysis results file."
    )
    parser.add_argument(
        "--analysis-results",
        type=str,
        default=None,
        help="Path to the analysis results JSON file used by --fix-only."
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


# Main Function
def main() -> int:
    """
    Main entry point of the application.
    """
    parser = _create_argument_parser()
    args = parser.parse_args()
    _validate_arguments(args, parser)

    try:
        api_key = _get_api_key(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    client = OpenAI(api_key=api_key)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    try:
        if args.project_path:
            _handle_local_project(args, client, timestamp)
            return 0

        _process_web_url(args, client, timestamp)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

# Local Project Router
def _handle_local_project(args, client, timestamp: str) -> None:
    """Delegate the local project workflow to the dedicated project flow module."""
    execute_local_project_flow(args, client, timestamp, _create_run_path)


# Web URL Flow
def _process_web_url(args, client, timestamp: str) -> None:
    """Delegate the public URL workflow to the dedicated web flow module."""
    execute_web_url_flow(args, client, timestamp, _create_run_path)



if __name__ == "__main__":
    sys.exit(main())