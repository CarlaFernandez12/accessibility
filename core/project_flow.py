"""
Local project orchestration flow.

This module encapsulates Angular and React project flows, including analyze-only,
fix-only, and full remediation modes, while keeping main.py as a thin entrypoint.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.ports import detect_react_dev_server_port
from utils.io_utils import clear_openai_logs, save_openai_logs, setup_directories



def _load_react_dependencies():
    from core.react_handler import (
        detect_react_project,
        fix_react_components_with_axe_violations,
        map_axe_violations_to_react_components,
        run_axe_on_react_app,
        start_react_dev_server,
    )

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



def execute_local_project_flow(args, client, timestamp: str, create_run_path: Callable[[str, str], str]) -> None:
    """Dispatch the local project flow for Angular or React projects."""
    project_path = os.path.abspath(args.project_path)

    is_react = (
        _detect_react_project_safe(project_path)
        or args.react_axe
        or args.react_axe_only
    )
    force_angular = args.angular_axe or args.angular_axe_only

    if args.analyze_only:
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
            react_url = args.react_url
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
            print("You must provide --analysis-results with the path to the analysis JSON file.")
            return
        with open(args.analysis_results, "r", encoding="utf-8") as file:
            axe_results = json.load(file)
        run_path = create_run_path(os.path.basename(project_path), timestamp)
        os.makedirs(run_path, exist_ok=True)
        if is_react:
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

    save_openai_logs(run_path)
    elapsed = int((datetime.now() - start_time).total_seconds())
    print(f"Total Angular execution time: {elapsed}s")
