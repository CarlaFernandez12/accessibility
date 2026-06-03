# Accessibility Analyzer

This project analyzes and remediates accessibility issues in public websites, Angular applications, and React applications. It combines Selenium, axe-core, and LLM-assisted fixes while preserving the existing UI and workflow behavior.

## Requirements

- Python 3.10 or newer
- Google Chrome
- Node.js and npm for Angular or React projects
- An OpenAI API key

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set the API key before running the tool:

```bash
export OPENAI_API_KEY="your_api_key"
```

## Main workflows

The CLI supports three modes:

1. Public URL analysis and HTML remediation.
2. Angular project remediation, with optional live Axe validation against a local dev server.
3. React project remediation, with violation mapping back to JSX and TSX source files.

Each run creates a timestamped directory under `results/`.

## CLI usage

Analyze a public page and generate a corrected version:

```bash
python main.py --url "https://example.com"
```

Analyze only and save `axe_results.json` without applying fixes:

```bash
python main.py --url "https://example.com" --analyze-only
python main.py --project-path "/path/to/project" --analyze-only --react-url "http://localhost:3000"
python main.py --project-path "/path/to/project" --analyze-only --angular-url "http://localhost:4200"
```

Apply fixes from an existing analysis file without re-running Axe:

```bash
python main.py --url "https://example.com" --fix-only --analysis-results "path/to/axe_results.json"
python main.py --project-path "/path/to/project" --fix-only --analysis-results "path/to/axe_results.json" --react-url "http://localhost:3000"
python main.py --project-path "/path/to/project" --fix-only --analysis-results "path/to/axe_results.json" --angular-url "http://localhost:4200"
```

Disable automatic dynamic interactions for public URLs:

```bash
python main.py --url "https://example.com" --disable-dynamic
```

## Angular projects

Typical Angular run:

```bash
python main.py --project-path "/path/to/angular-project"
```

Useful flags:

```bash
python main.py --project-path "/path/to/angular-project" --angular-axe
python main.py --project-path "/path/to/angular-project" --angular-url "http://localhost:4300"
python main.py --project-path "/path/to/angular-project" --serve-app
```

## React projects

Typical React run:

```bash
python main.py --project-path "/path/to/react-project" --react-axe
```

If the dev server is not on the default port:

```bash
python main.py --project-path "/path/to/react-project" --react-axe --react-url "http://localhost:4300"
```

## Output structure

Common output files include:

- `axe_results.json`: raw accessibility analysis output.
- `accessible_page.html`: remediated HTML for public URL flows.
- `comparison_report.html`: before/after comparison report when a full web flow completes.
- `openai_logs.json`: prompts and responses captured for the run.
- `color_catalog.json`: extracted project color catalog when available.

## Repository structure

- `main.py`: CLI entrypoint.
- `core/project_flow.py`: local Angular and React orchestration.
- `core/web_flow.py`: public URL orchestration.
- `core/analyzer.py`: Selenium and axe-core execution.
- `core/html_generator.py`: public HTML remediation flow.
- `core/angular_handler.py`: Angular remediation orchestration.
- `core/react_handler.py`: React violation mapping and remediation.
- `utils/`: shared filesystem, HTML, color-catalog, and report helpers.

## Notes

- Runs are isolated by timestamp to keep outputs comparable.
- Angular and React projects usually need dependencies installed before running the tool.
- `--serve-app` can start the local app automatically when the project supports it.

