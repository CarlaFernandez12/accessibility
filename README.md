# Accessibility Analyzer

This project automates the process of detecting and fixing common
accessibility issues in web applications. It combines automated testing
(axe-core + Selenium) with LLM-assisted remediation to generate improved
versions of existing pages.

The tool can analyse **static websites** as well as applications built
with **Angular** and **React**.

------------------------------------------------------------------------

# Repository Setup

Clone the repository before running the tool:

``` bash
git clone https://github.com/CarlaFernandez12/accessibility
cd accessibility
```

------------------------------------------------------------------------

# Prerequisites

Make sure the following software is installed on your system:

-   **Python 3.10 or newer**
-   **Google Chrome** (required for Selenium)
-   **Node.js and npm** (required when analysing Angular or React
    projects)
-   A valid **OpenAI API key**

------------------------------------------------------------------------

# Installation

Create and activate a virtual environment.

### Linux / macOS

``` bash
python3 -m venv .venv
source .venv/bin/activate
```

### Windows (PowerShell)

``` powershell
python -m venv .venv
.venv\Scripts\activate
```

Install the Python dependencies:

``` bash
pip install -r requirements.txt
```

------------------------------------------------------------------------

# Separate Analysis and Fix-Only Execution

From the current version, you can run the analysis and the fixing as two completely independent steps:

**1. Analysis only (does not modify anything, only generates the JSON with detected issues):**

```bash
python main.py --project-path "/path/to/project" --analyze-only --react-url "http://localhost:3000"
python main.py --project-path "/path/to/project" --analyze-only --angular-url "http://localhost:4200"

# or for a public website
python main.py --url "https://example.com" --analyze-only
```

This generates an `axe_results.json` file with all detected issues, without modifying any source files or HTML.

**2. Fix only (uses the generated JSON, does not re-analyze):**

```bash
python main.py --project-path "/path/to/project" --fix-only --analysis-results "path/to/axe_results.json" --react-url "http://localhost:3000"
python main.py --project-path "/path/to/project" --fix-only --analysis-results "path/to/axe_results.json" --angular-url "http://localhost:4200"

# or for a public website
python main.py --url "https://example.com" --fix-only --analysis-results "path/to/axe_results.json"
```

This applies the necessary fixes using the analysis JSON, generating the accessible version.

**Important:** If you do not use either of these flags, the process will be combined (analysis + fixing, as before).

------------------------------------------------------------------------

# Automatic Correction of Icon-Only Buttons

The system automatically detects and fixes buttons that only contain icons (e.g., trash, add, close, search, etc.) and do not have visible text or an aria-label.

For these cases, an appropriate `aria-label` attribute is added based on the detected icon. Example:

```html
<button class="btn btn-outline-dark"><i class="bi bi-trash3"></i></button>
```

Is automatically converted to:

```html
<button class="btn btn-outline-dark" aria-label="Delete"><i class="bi bi-trash3"></i></button>
```

This improves accessibility for screen readers and complies with WCAG requirements.

------------------------------------------------------------------------

# OpenAI API Key

The tool requires an OpenAI API key.

### Linux / macOS

``` bash
export OPENAI_API_KEY="your_api_key"
```

### Windows (PowerShell)

``` powershell
$env:OPENAI_API_KEY="your_api_key"
```

### Windows (Command Prompt)

``` cmd
set OPENAI_API_KEY=your_api_key
```

------------------------------------------------------------------------

# Overview

The tool supports three main workflows:

1.  **Public web pages** -- analyse a URL and generate a corrected HTML
    version with improved accessibility.
2.  **Angular applications** -- inspect Angular templates and optionally
    validate fixes against a running development server.
3.  **React applications** -- analyse a running React application and
    map accessibility issues back to JSX/TSX components.

Each execution generates a dedicated output directory containing the
accessibility reports and corrected files.

------------------------------------------------------------------------

# Static Website Analysis

Use this mode to analyse any public web page.

``` bash
python main.py --url "https://example.com"
```

Optional flag to disable interactions with dynamic elements:

``` bash
python main.py --url "https://example.com" --disable-dynamic
```

During execution the tool will:

-   Load the page using Selenium and headless Chrome
-   Run **axe-core** to detect accessibility violations
-   Use an LLM to propose HTML improvements
-   Generate an accessible version of the page
-   Produce a comparison report

Once the process finishes, the CLI will ask whether to start a **local
preview server** to open the corrected page.

------------------------------------------------------------------------

# Angular Project Analysis

### 1. Clone and prepare the Angular project

``` bash
git clone <angular-project-repository>
cd <angular-project>
```

Install dependencies:

``` bash
npm install
```

Start the development server:

``` bash
npm start
```

or

``` bash
ng serve
```

### 2. Run the analyzer

Open another terminal and move to the **accessibility tool repository**:

``` bash
cd accessibility
```

Run the analysis:

``` bash
python main.py --project-path "/path/to/angular-project"
```

Optional flags:

``` bash
python main.py --project-path "/path/to/angular-project" --angular-axe
```

``` bash
python main.py --project-path "/path/to/angular-project" --angular-url "http://localhost:4300"
```

``` bash
python main.py --project-path "/path/to/angular-project" --serve-app
```

------------------------------------------------------------------------

# React Project Analysis

### 1. Clone and prepare the React project

``` bash
git clone <react-project-repository>
cd <react-project>
```

Install dependencies:

``` bash
npm install
```

Start the development server:

``` bash
npm start
```

or

``` bash
npm run dev
```

### 2. Run the analyzer

From the **accessibility tool repository**:

``` bash
cd accessibility
```

Run the analysis:

``` bash
python main.py --project-path "/path/to/react-project" --react-axe
```

If the application runs on a different port:

``` bash
python main.py --project-path "/path/to/react-project" --react-axe --react-url "http://localhost:4300"
```

------------------------------------------------------------------------

# Generated Results

Each execution creates a new directory inside `results/` containing the
analysis output.

Example structure:

    results/
     ├─ example_com/
     │   └─ 2026_03_16_14_20/
     │       ├─ original_page.html
     │       ├─ accessible_page.html
     │       ├─ initial_report.json
     │       ├─ final_report.json
     │       ├─ comparison_report.html
     │       └─ screenshots/

Main files:

-   **original_page.html** -- HTML captured from the original page
-   **accessible_page.html** -- version with applied accessibility
    improvements
-   **initial_report.json** -- raw axe-core report before fixes
-   **final_report.json** -- axe-core report after corrections
-   **comparison_report.html** -- summary comparing before and after
    results

------------------------------------------------------------------------

# Notes

-   The tool relies on **Google Chrome** to run automated accessibility
    checks.
-   Angular and React projects normally need to be running locally
    before analysis unless the `--serve-app` flag is used.
-   Each run is stored separately to make it easier to compare results
    across executions.

