Accessibility AI Assistant
===========================

AI‑assisted web accessibility analysis and remediation tool.  
This project automates end‑to‑end accessibility workflows:

- Crawls a web page (or a running Angular/React app) with Selenium.
- Runs axe‑core to detect accessibility violations.
- Uses LLMs to propose and apply HTML/JSX/Angular template fixes.
- Regenerates an accessible version of the page and a comparison report.

The codebase is structured for clarity and for use as a technical reference or article.

Overview
--------

The tool supports **three main workflows**:

1. **Public web page URL**
   - Input: `--url "https://example.com"`
   - Flow:
     - Launch headless Chrome via Selenium.
     - Optionally perform safe dynamic interactions (cookies, modals, lazy content).
     - Run axe‑core on the original page.
     - Use an LLM to correct accessibility issues in the HTML:
       - Colour contrast.
       - Missing `alt` / `aria-*` attributes.
       - Link/button naming issues.
     - Optionally use screenshots to preserve responsive layout.
     - Generate corrected `accessible_page.html`.
     - Run axe‑core again on the corrected page.
     - Generate `comparison_report.html` (before/after summary + execution time).
     - Optionally start a small local HTTP server to preview the corrected page.

2. **Angular projects**
   - Input: `--project-path "/path/to/angular-project"`  
     With optional `--angular-axe` to enable the advanced Angular + Axe flow.
   - Flow:
     - Use `angular.json` to locate source roots.
     - Analyse Angular templates and map axe‑core violations back to them.
     - Use the LLM to propose template‑level fixes.
     - Optionally run a second axe‑core pass against a running Angular dev server.

3. **React projects**
   - Input: `--project-path "/path/to/react-project" --react-axe`
   - Flow:
     - Detect React usage via `package.json` and/or JSX/TSX files.
     - Detect the dev server port or use `--react-url`.
     - Run axe‑core against the running React dev server.
     - Map violations back to components and apply LLM‑driven JSX/TSX fixes.

Architecture
------------

**Entry point**

- `main.py`  
  Central CLI entry point and orchestrator:
  - Parses arguments (`--url`, `--project-path`, `--angular-axe`, `--react-axe`, etc.).
  - Routes to:
    - Web URL workflow (`_process_web_url`).
    - Angular workflow (`_process_angular_project`).
    - React workflow (`_process_react_project_flow`).
  - Creates per‑run output directories under `results/`.
  - Manages preview HTTP server for `accessible_page.html`.

**Configuration**

- `config/constants.py`  
  Global constants such as:
  - `BASE_RESULTS_DIR`
  - `CACHE_DIR`
  - `AXE_SCRIPT_URL`

**Core logic**

- `core/analyzer.py`
  - Integration layer between Selenium and axe‑core.
  - Loads the axe script into the page and executes it.
  - Handles SSL warning pages.
  - Supports:
    - Single‑state analysis (`run_axe_analysis`).
    - Multi‑state analysis (`run_axe_analysis_multiple_states`).

- `core/dynamic_handler.py`
  - Safe, minimal dynamic interactions:
    - Accept cookie banners.
    - Close simple modal/popups.
    - Trigger lazy‑loaded content via scrolling.
  - Optional custom interaction scripts.

- `core/html_generator.py`
  - Accessible HTML generation and post‑processing:
    - Contrast analysis and colour suggestions (WCAG‑based).
    - Prompt construction for the LLM (HTML fragments + rules).
    - Application of fragment‑level fixes back into the DOM.
    - Final “responsive merge” between original and corrected HTML while
      preserving accessibility attributes.

- `core/image_processing.py`
  - Extracts `<img>` elements from the page.
  - Downloads images (with SSL fallback).
  - Uses a vision‑capable model to generate concise alt‑text descriptions.
  - Caches descriptions per image URL in `media_cache/cache.json`.

- `core/screenshot_handler.py`
  - Takes full‑page screenshots for common viewports (mobile/tablet/desktop).
  - Can generate a simple HTML gallery of screenshots for visual review.

- `core/webdriver_setup.py`
  - Configures and creates a headless Chrome WebDriver.
  - Uses `webdriver-manager` when no local `chromedriver` is found.
  - Sets generous timeouts and relaxed SSL/mixed‑content handling for robustness.

- `core/ports.py`
  - Port and dev‑server detection helpers.
  - Probes common dev ports (3000, 5173, 8080, …) to find a running React app.

- `core/angular_handler.py`
  - Angular‑specific workflows:
    - Reads `angular.json` and discovers template files.
    - Runs axe‑core against a running Angular dev server.
    - Maps violations back to Angular templates.
    - Calls the LLM to produce template fixes.
    - Optionally performs automatic contrast fixes (feature‑flagged).

- `core/react_handler.py`
  - React‑specific workflows:
    - Detects React usage via `package.json` and source files.
    - Runs axe‑core against a running React dev server.
    - Maps violations to JSX/TSX components.
    - Calls the LLM to propose component‑level fixes.

- `core/report.py`
  - Generates a static `comparison_report.html`:
    - Initial vs final violation counts.
    - Number of fixed violations.
    - Relative improvement (%).
    - Total execution time.

**Utilities**

- `utils/io_utils.py`
  - Directory setup for runs and cache.
  - Global logging buffer for OpenAI calls.
  - Helpers to persist logs and cache.
  - Image → base64 conversion.

- `utils/violation_utils.py`
  - Grouping and flattening of axe‑core violations.
  - Priority ordering by severity and ID.
  - Contrast‑specific extraction from violation data.

- `utils/html_utils.py`
  - Conversion of relative asset URLs to absolute URLs based on a base URL.

Outputs
-------

For each run, a new directory is created under `results/`, for example:

- `results/<sanitized-target>/<timestamp>/`
  - `original_page.html` – unmodified HTML (URL workflows).
  - `accessible_page.html` – corrected HTML ready for preview.
  - `initial_report.json` / `final_report.json` – raw axe‑core results.
  - `comparison_report.html` – human‑readable before/after summary.
  - `openai_logs.json` – detailed prompts/responses (debugging).
  - `screenshots/before/*.png` – responsive screenshots if enabled.

Installation
------------

Prerequisites:

- Python 3.10+ (recommended).
- Google Chrome (or Chromium) installed.

Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\\Scripts\\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set your OpenAI API key:

```bash
export OPENAI_API_KEY="sk-..."    # Linux / macOS
setx OPENAI_API_KEY "sk-..."      # Windows (new shell required)
```

Usage
-----

### Analyse a public web page

```bash
python main.py --url "https://example.com"
```

The tool will:

- Run the analysis and corrections.
- Generate `accessible_page.html` and `comparison_report.html` under `results/`.
- Ask if you want to preview the corrected page in the browser.

### Analyse an Angular project (classic flow)

```bash
python main.py --project-path "/path/to/angular-project"
```

### Analyse an Angular project with Axe over a running dev server

In one terminal:

```bash
cd /path/to/angular-project
ng serve
```

In another terminal, from this repo:

```bash
python main.py --project-path "/path/to/angular-project" --angular-axe
```

### Analyse a React project with Axe

Start your React dev server first (e.g. `npm start` or `npm run dev`).  
Then run:

```bash
python main.py --project-path "/path/to/react-project" --react-axe
```

If the dev server is not on a common port, provide it explicitly:

```bash
python main.py --project-path "/path/to/react-project" \
  --react-axe --react-url "http://localhost:4300/"
```

Project goals and non‑goals
---------------------------

Goals:

- Provide an end‑to‑end, reproducible accessibility improvement workflow.
- Showcase how axe‑core and LLMs can be combined safely.
- Serve as a clean, well‑structured reference implementation for articles or talks.

Non‑goals:

- Being a generic crawler or performance testing tool.
- Providing full test coverage of every edge case in arbitrary sites.
- Replacing manual accessibility audits by experts.

Contributing / Extending
------------------------

- New flows (e.g. Vue, Svelte) can be added under `core/` following the
  patterns in `angular_handler.py` and `react_handler.py`.
- Prompt text lives **inside the handlers and `html_generator.py`**; changes
  there should be made carefully to avoid regressions.
- For more robustness, consider adding:
  - `pytest` + a small test suite for helpers and sample HTML.
  - `mypy` + `ruff` (or flake8) and `pre-commit` hooks.

