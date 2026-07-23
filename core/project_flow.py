"""
Local project orchestration flow.

This module encapsulates Angular and React project flows, including analyze-only,
fix-only, and full remediation modes, while keeping main.py as a thin entrypoint.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict

from core.html_generator import generate_accessible_html_with_parser
from core.ports import detect_react_dev_server_port
from utils.violation_utils import flatten_violations
from utils.io_utils import clear_openai_logs, save_openai_logs, setup_directories



def _load_react_dependencies():
    from core.react_handler import (
        fix_react_components_with_axe_violations,
        map_axe_violations_to_react_components,
    )
    from core.react_support import detect_react_project, run_axe_on_react_app, start_react_dev_server

    return {
        "detect_react_project": detect_react_project,
        "fix_react_components_with_axe_violations": fix_react_components_with_axe_violations,
        "map_axe_violations_to_react_components": map_axe_violations_to_react_components,
        "run_axe_on_react_app": run_axe_on_react_app,
        "start_react_dev_server": start_react_dev_server,
    }



def _load_angular_dependencies():
    from core.angular_handler import (
        fix_angular_project_from_axe_results,
        process_angular_project,
        run_axe_on_angular_app,
    )

    return {
        "fix_angular_project_from_axe_results": fix_angular_project_from_axe_results,
        "process_angular_project": process_angular_project,
        "run_axe_on_angular_app": run_axe_on_angular_app,
    }



def _detect_react_project_safe(project_path: str) -> bool:
    try:
        react_dependencies = _load_react_dependencies()
    except ModuleNotFoundError as exc:
        print(f"[Detection] React support unavailable: {exc}")
        return False

    return react_dependencies["detect_react_project"](project_path)


def _detect_angular_project_safe(project_path: str) -> bool:
    """Heuristic Angular detection used to disambiguate mixed Angular/React signals."""
    try:
        project_root = Path(project_path)
        if (project_root / "angular.json").exists():
            return True

        package_json = project_root / "package.json"
        if package_json.exists():
            with open(package_json, "r", encoding="utf-8") as file:
                data = json.load(file)
            deps = {
                **(data.get("dependencies", {}) or {}),
                **(data.get("devDependencies", {}) or {}),
            }
            if any(name.startswith("@angular/") for name in deps.keys()):
                return True

        # Fallback to common Angular file patterns.
        if any(project_root.glob("**/*.component.ts")):
            return True
        if any(project_root.glob("**/*.component.html")):
            return True
    except Exception:
        return False

    return False


def _detect_html_project_safe(project_path: str) -> bool:
    """Return True when a project contains standalone HTML files."""
    try:
        project_root = Path(project_path)
        html_files = [
            path for path in project_root.glob("**/*.html")
            if all(part not in {"node_modules", "dist", "build", "results"} for part in path.parts)
        ]
        return bool(html_files)
    except Exception:
        return False


def _fix_local_html_project_from_axe_results(project_path: str, axe_results: Dict, client) -> int:
    """Apply HTML fixes directly to local project files from saved Axe results."""
    project_root = Path(project_path)
    html_files = [
        path for path in project_root.glob("**/*.html")
        if all(part not in {"node_modules", "dist", "build", "results"} for part in path.parts)
    ]
    if not html_files:
        print("[HTML] No HTML files found in the project.")
        return 0

    flat_violations = flatten_violations(axe_results.get("violations", []))
    if not flat_violations:
        print("[HTML] No actionable violations found in the analysis results.")
        return 0

    fixed_files = 0
    for html_file in html_files:
        try:
            original_html = html_file.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[HTML] Warning: failed to read {html_file}: {exc}")
            continue

        normalized_html = original_html.lower()
        matching_violations = []
        for violation in flat_violations:
            snippet = (violation.get("html_snippet") or "").strip().lower()
            selector = (violation.get("selector") or "").strip().lower()
            if snippet and snippet[:120] in normalized_html:
                matching_violations.append(violation)
                continue
            if selector and selector not in {"no selector", "html", "body"} and selector in normalized_html:
                matching_violations.append(violation)

        if not matching_violations:
            continue

        generated_html = generate_accessible_html_with_parser(
            original_html,
            {"violations": matching_violations},
            [],
            client,
            html_file.parent.as_uri() + "/",
            None,
            [],
            convert_relative_paths=False,
        )

        if generated_html != original_html:
            html_file.write_text(generated_html, encoding="utf-8")
            fixed_files += 1
            print(f"[HTML] Fixed file: {html_file.relative_to(project_root)}")

    return fixed_files



def execute_local_project_flow(args, client, timestamp: str, create_run_path: Callable[[str, str], str]) -> None:
    """Dispatch the local project flow for Angular or React projects."""
    project_path = os.path.abspath(args.project_path)

    is_react = (
        _detect_react_project_safe(project_path)
        or args.react_axe
        or args.react_axe_only
    )
    is_angular = _detect_angular_project_safe(project_path)
    is_html = _detect_html_project_safe(project_path)
    force_react = args.react_axe or args.react_axe_only
    force_angular = args.angular_axe or args.angular_axe_only

    if args.project_type == "angular":
        is_angular, is_react, is_html = True, False, False
    elif args.project_type == "react":
        is_angular, is_react, is_html = False, True, False
    elif args.project_type == "html":
        is_angular, is_react, is_html = False, False, True

    # If both frameworks are detected, prefer Angular unless React mode is explicitly forced.
    if is_angular and is_react and not force_react and not force_angular:
        print("[Detection] Mixed Angular/React signals detected; defaulting to Angular flow.")
        is_react = False

    if is_html and not is_react and not is_angular and not args.fix_only:
        raise ValueError(
            "Local HTML projects currently support only --fix-only with --analysis-results. "
            "Use --project-type html --fix-only --analysis-results <axe_results.json>."
        )

    if args.analyze_only:
        print("[Mode] analyze-only enabled: this run only collects Axe results and color catalog, it does not apply fixes.")
        run_path = create_run_path(os.path.basename(project_path), timestamp)
        setup_directories(run_path)
        if force_angular or not is_react:
            print(f"[Detection] Project treated as Angular: {project_path}")
            angular_dependencies = _load_angular_dependencies()
            axe_results = angular_dependencies["run_axe_on_angular_app"](args.angular_url, run_path)
            results_path = os.path.join(run_path, "axe_results.json")
            with open(results_path, "w", encoding="utf-8") as file:
                json.dump(axe_results, file, ensure_ascii=False, indent=2)
            print(f"Analysis completed. Results saved to: {results_path}")
        elif is_react:
            print(f"[Detection] React project detected: {project_path}")
            react_dependencies = _load_react_dependencies()
            detected_port = detect_react_dev_server_port(project_path)
            default_react_url = "http://localhost:3000/"
            default_angular_url = "http://localhost:4200/"

            if args.react_url and args.react_url != default_react_url:
                react_url = args.react_url
            elif args.angular_url and args.angular_url != default_angular_url:
                # Backward-compatible fallback for users passing --angular-url on React projects.
                react_url = args.angular_url
            elif detected_port:
                react_url = f"http://localhost:{detected_port}/"
            else:
                react_url = args.react_url

            print(f"[React + Axe] analyze-only URL: {react_url}")
            axe_results, _ = react_dependencies["run_axe_on_react_app"](
                react_url,
                run_path,
                suffix="_before",
                take_screenshots_flag=True,
            )
            results_path = os.path.join(run_path, "axe_results.json")
            with open(results_path, "w", encoding="utf-8") as file:
                json.dump(axe_results, file, ensure_ascii=False, indent=2)
            print(f"Analysis completed. Results saved to: {results_path}")

        from utils.color_catalog_utils import extract_color_catalog

        extract_color_catalog(project_path, run_path)

        return

    if args.fix_only:
        if not args.analysis_results or not os.path.exists(args.analysis_results):
            raise ValueError("You must provide --analysis-results with the path to the analysis JSON file.")
        with open(args.analysis_results, "r", encoding="utf-8") as file:
            axe_results = json.load(file)
        run_path = create_run_path(os.path.basename(project_path), timestamp)
        os.makedirs(run_path, exist_ok=True)
        if is_html and not is_react and not is_angular:
            print("[fix-only] Fixing local HTML files...")
            fixed_files = _fix_local_html_project_from_axe_results(project_path, axe_results, client)
            print(f"[HTML] Files fixed: {fixed_files}")
            return

        if is_react and not (is_angular and not force_react and not force_angular):
            print("[fix-only] Fixing React source files...")
            react_dependencies = _load_react_dependencies()
            issues_by_component = react_dependencies["map_axe_violations_to_react_components"](
                axe_results,
                Path(project_path),
            )
            if issues_by_component:
                fixes = react_dependencies["fix_react_components_with_axe_violations"](
                    issues_by_component,
                    Path(project_path),
                    client,
                    analysis_results_path=args.analysis_results,
                )
                print(f"[React + Axe] Components fixed: {len(fixes)}")
            else:
                print("[React + Axe] No violations mapped to components.")
            return

        print("[fix-only] Fixing Angular source files...")
        angular_dependencies = _load_angular_dependencies()
        angular_dependencies["fix_angular_project_from_axe_results"](
            project_path,
            axe_results,
            client,
            run_path,
            args.analysis_results,
        )
        return

    if force_angular:
        print(f"[Detection] Project treated as Angular: {project_path}")
        _process_angular_project(args, client, timestamp, create_run_path)
    elif is_react:
        print(f"[Detection] React project detected: {project_path}")
        _process_react_project_flow(args, client, timestamp, create_run_path)
    else:
        _process_angular_project(args, client, timestamp, create_run_path)



def _process_react_project_flow(args, client, timestamp: str, create_run_path: Callable[[str, str], str]) -> None:
    """Execute the advanced React + Axe flow."""
    project_path = os.path.abspath(args.project_path)
    project_name = os.path.basename(project_path.rstrip(os.sep)) or "react_project"

    run_path = create_run_path(project_name, timestamp)
    setup_directories(run_path)
    clear_openai_logs()

    react_axe_enabled = args.react_axe or args.react_axe_only
    dev_server_process = None

    try:
        react_dependencies = _load_react_dependencies()

        if args.serve_app:
            print("[React + serve-app] Starting React dev server...")
            dev_server_process = react_dependencies["start_react_dev_server"](Path(project_path))
            if dev_server_process:
                print("[React + serve-app] ✓ Dev server started.")
            else:
                print("[React + serve-app] → Server may already be running or could not be started.")

        if react_axe_enabled:
            detected_port = detect_react_dev_server_port(project_path)
            react_url = f"http://localhost:{detected_port}/" if detected_port else args.react_url
            print(f"[React + Axe] Executing analysis on: {react_url}")

            try:
                axe_results, screenshot_paths = react_dependencies["run_axe_on_react_app"](
                    react_url,
                    run_path,
                    suffix="_before",
                    take_screenshots_flag=True,
                )
                issues_by_component = react_dependencies["map_axe_violations_to_react_components"](
                    axe_results,
                    Path(project_path),
                )

                if issues_by_component:
                    fixes = react_dependencies["fix_react_components_with_axe_violations"](
                        issues_by_component,
                        Path(project_path),
                        client,
                        screenshot_paths=screenshot_paths,
                        analysis_results_path=os.path.join(run_path, "axe_results.json"),
                    )
                    print(f"[React + Axe] Components fixed: {len(fixes)}")
                else:
                    print("[React + Axe] No violations mapped to components.")
            except Exception as exc:
                print(f"[React + Axe] Error: {exc}")
                raise
    finally:
        if dev_server_process is not None:
            print("[React + serve-app] Stopping dev server...")
            dev_server_process.terminate()
            print("[React + serve-app] ✓ Dev server stopped.")

    save_openai_logs(run_path)
    print("React workflow completed.")



def _process_angular_project(args, client, timestamp: str, create_run_path: Callable[[str, str], str]) -> None:
    """Process Angular project (classic + advanced Axe flow)."""
    project_path = os.path.abspath(args.project_path)
    project_name = os.path.basename(project_path.rstrip(os.sep)) or "angular_project"

    run_path = create_run_path(project_name, timestamp)
    start_time = datetime.now()

    setup_directories(run_path)
    clear_openai_logs()

    try:
        angular_dependencies = _load_angular_dependencies()
        summary = angular_dependencies["process_angular_project"](
            project_path,
            client,
            run_path,
            serve_app=args.serve_app,
        )
        if summary:
            print("\n--- Angular summary ---")
            for line in summary:
                print(line)
    except Exception as exc:
        print(f"Error processing Angular project: {exc}")
        raise

    save_openai_logs(run_path)
    elapsed = int((datetime.now() - start_time).total_seconds())
    print(f"Total Angular execution time: {elapsed}s")
