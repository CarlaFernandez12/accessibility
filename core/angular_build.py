"""
Angular build and dev-server helpers extracted from the legacy monolithic handler.

This module keeps compilation verification, compilation-fix prompting, and
Angular dev-server startup isolated from the high-level orchestration layer.
"""

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.angular_support import get_default_project_name
from utils.io_utils import log_openai_call


def _load_package_scripts(project_root: Path) -> Dict[str, str]:
    """Return the scripts section from package.json when available."""
    package_json = project_root / "package.json"
    if not package_json.exists():
        return {}

    with open(package_json, "r", encoding="utf-8") as file:
        package_data = json.load(file)
    return package_data.get("scripts", {})


def _resolve_node_modules_ng_command(project_root: Path) -> Optional[str]:
    """Resolve the Angular CLI executable under node_modules when present."""
    bin_dir = project_root / "node_modules" / ".bin"
    ng_cmd = bin_dir / "ng.cmd"
    if ng_cmd.exists():
        return str(ng_cmd)

    ng_bat = bin_dir / "ng.bat"
    if ng_bat.exists():
        return str(ng_bat)

    ng_script = bin_dir / "ng"
    if not ng_script.exists():
        return None

    import sys

    if sys.platform != "win32":
        return None

    fallback_cmd = bin_dir / "ng.cmd"
    if fallback_cmd.exists():
        return str(fallback_cmd)

    try:
        with open(ng_script, "r", encoding="utf-8") as file:
            first_line = file.readline()
            if first_line.startswith("#!"):
                return None
    except Exception:
        return None

    return str(fallback_cmd) if fallback_cmd.exists() else None


def _run_build_verification_command(project_root: Path, build_cmd: List[str]) -> Tuple[bool, bool]:
    """Execute a build verification command and normalize its result."""
    result = subprocess.run(
        build_cmd,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode == 0:
        return True, True
    if result.stderr:
        print(f"  Compilation errors:\n{result.stderr[:500]}")
    return False, True


def _run_build_and_collect_errors(
    project_root: Path,
    build_cmd: List[str],
) -> Tuple[str, bool, List[str], bool]:
    """Execute a build command and return output, availability, parsed errors, and success."""
    result = subprocess.run(
        build_cmd,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    output = result.stderr + result.stdout
    errors = _parse_angular_errors(output)
    success = result.returncode == 0 and not errors
    return output, True, errors, success



def _verify_angular_build(project_root: Path) -> Tuple[bool, bool]:
    """
    Verify that the Angular project compiles correctly by executing ng build.

    Returns:
        Tuple of (compilation success, verification available).
        If verification is not available, returns (True, False) to avoid blocking.
    """
    default_project = get_default_project_name(project_root)
    project_arg = [default_project] if default_project else []
    if default_project:
        print(f"  Multi-project workspace detected, building: {default_project}")

    try:
        scripts = _load_package_scripts(project_root)
        if "build" in scripts:
            print("  → Using 'npm run build' to verify compilation...")
            return _run_build_verification_command(project_root, ["npm", "run", "build"])
    except Exception:
        pass

    try:
        build_cmd = ["ng", "build"] + project_arg + ["--configuration", "production"]
        return _run_build_verification_command(project_root, build_cmd)
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        print("  Warning: timed out while compiling the project.")
        return False, True
    except Exception:
        pass

    try:
        build_cmd = ["npx", "-y", "@angular/cli", "build"] + project_arg + ["--configuration", "production"]
        return _run_build_verification_command(project_root, build_cmd)
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        print("  Warning: timed out while compiling the project.")
        return False, True
    except Exception:
        pass

    node_modules_ng = project_root / "node_modules" / ".bin" / "ng"
    if node_modules_ng.exists():
        try:
            ng_cmd = str(node_modules_ng)
            if not ng_cmd.endswith(".cmd") and (project_root / "node_modules" / ".bin" / "ng.cmd").exists():
                ng_cmd = str(project_root / "node_modules" / ".bin" / "ng.cmd")

            build_cmd = [ng_cmd, "build"] + project_arg + ["--configuration", "production"]
            return _run_build_verification_command(project_root, build_cmd)
        except Exception as exc:
            print(f"  ⚠️ Error running ng from node_modules: {exc}")

    print("  ⚠️ Could not run ng build (ng not found in PATH, npx not available, or node_modules not found)")
    print("  → Continuing without compilation verification")
    return True, False



def _compile_and_get_errors(project_root: Path) -> Dict:
    """
    Compile the Angular project and return compilation errors if any.
    """
    errors: List[str] = []
    output = ""
    success = True
    verification_available = False

    try:
        try:
            scripts = _load_package_scripts(project_root)
            if "build" in scripts:
                output, verification_available, errors, success = _run_build_and_collect_errors(
                    project_root,
                    ["npm", "run", "build"],
                )
                if errors:
                    print(f"  → Build completed but {len(errors)} errors found, parsing...")
                elif not success:
                    print("  → Build failed, parsing errors...")
        except Exception as exc:
            print(f"  Warning: failed to run 'npm run build': {exc}")

        if not verification_available or (not errors and not success):
            try:
                default_project = get_default_project_name(project_root)
                build_cmd = ["ng", "build"]
                if default_project:
                    build_cmd.append(default_project)
                    print(f"  Multi-project workspace detected, building project: {default_project}")

                output, verification_available, parsed_errors, success = _run_build_and_collect_errors(
                    project_root,
                    build_cmd,
                )
                if not errors:
                    errors = parsed_errors
                    if errors:
                        print(f"  → Build completed but {len(errors)} errors found, parsing...")
                    elif not success:
                        print("  → Build failed, parsing errors...")
            except Exception as exc:
                print(f"  Warning: failed to run 'ng build': {exc}")
    except Exception as exc:
        print(f"  Warning: unexpected error while collecting compilation errors: {exc}")

    if not verification_available:
        success, verification_available = _verify_angular_build(project_root)

    return {
        "success": success,
        "verification_available": verification_available,
        "errors": errors,
        "output": output,
    }



def _parse_angular_errors(build_output: str) -> List[str]:
    """Parse Angular compilation errors from the build output."""
    errors: List[str] = []
    lines = build_output.split("\n")

    current_error: List[str] = []
    in_error_block = False

    for line in lines:
        is_error_line = (
            "ERROR" in line.upper()
            or "error TS" in line.lower()
            or "error NG" in line.lower()
            or (
                line.strip().startswith("./src/")
                and (
                    "Error:" in line
                    or "Error" in line
                    or "Module not found" in line
                    or "Can't resolve" in line
                )
            )
            or "Module not found" in line
            or "Can't resolve" in line
            or "Cannot find module" in line
            or (line.strip().startswith("src/") and "error TS" in line.lower())
            or (line.strip().startswith("Error:") and ("TS" in line or "NG" in line))
        )

        if is_error_line:
            if current_error:
                errors.append("\n".join(current_error))
                current_error = []
            current_error.append(line)
            in_error_block = True
        elif in_error_block:
            if line.strip() == "" and current_error:
                if len(current_error) > 1:
                    current_error.append(line)
                else:
                    errors.append("\n".join(current_error))
                    current_error = []
                    in_error_block = False
            elif (
                line.strip().startswith("src/")
                or line.strip().startswith("./src/")
                or ":" in line
                or line.strip().startswith("Error occurs")
                or "error TS" in line.lower()
                or "error NG" in line.lower()
                or "Cannot find module" in line
                or "Can't resolve" in line
                or "imports:" in line
                or "import {" in line
            ):
                current_error.append(line)
            elif current_error and (line.strip() or "at " in line or "^" in line):
                current_error.append(line)
            else:
                errors.append("\n".join(current_error))
                current_error = []
                in_error_block = False

    if current_error:
        errors.append("\n".join(current_error))

    return [error for error in errors if error.strip()][:20]


def _request_angular_compilation_fix(
    file_path: str,
    original_content: str,
    file_errors: List[str],
    client,
) -> str:
    """Ask the LLM for an Angular compilation fix and normalize the returned code."""
    errors_text = "\n\n".join(file_errors[:3])
    system_message = "You are an expert in Angular and TypeScript. Fix the compilation errors without changing functionality."
    has_missing_module = (
        "Module not found" in errors_text
        or "Cannot find module" in errors_text
        or "Can't resolve" in errors_text
    )

    if has_missing_module:
        import re

        module_name = None
        module_match = re.search(
            r"Can't resolve '([^']+)'|Cannot find module '([^']+)'|Module not found.*'([^']+)'",
            errors_text,
        )
        if module_match:
            module_name = module_match.group(1) or module_match.group(2) or module_match.group(3)

        prompt = f"""
Fix the following Angular compilation errors in the file {file_path}:

Errors:
{errors_text}

IMPORTANT: The module '{module_name if module_name else "unknown"}' cannot be found or does not exist in npm.
You MUST do the following:
1. COMMENT OUT or REMOVE the import of the missing module
2. COMMENT OUT or REMOVE all uses of the module in the code (in @Component imports, in code, etc.)
3. If the module is used in the @Component imports array, REMOVE it from that array
4. Add an explanatory comment: // Module not available: {module_name if module_name else "missing module"}

Example:
- If you have: import {{CKEditorModule}} from "@angular/ckeditor5-angular";
- Change to: // import {{CKEditorModule}} from "@angular/ckeditor5-angular"; // Module not available
- And remove CKEditorModule from the @Component imports array

Current file content:
```typescript
{original_content[:3000]}
```

Fix ONLY the compilation errors. COMMENT OUT or REMOVE the import and ALL its uses.
Return the full corrected code without the missing module.
"""
    else:
        prompt = f"""
Fix the following Angular compilation errors in the file {file_path}:

Errors:
{errors_text}

Current file content:
```typescript
{original_content[:3000]}
```

Fix ONLY the compilation errors. Keep all existing functionality and logic.
Return the full corrected code.
"""

    response = client.chat.completions.create(
        model="gpt-5",
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
    )

    corrected_content = response.choices[0].message.content.strip()
    log_openai_call(
        prompt=prompt,
        response=corrected_content,
        model="gpt-5",
        call_type="angular_compilation_fix",
    )

    if corrected_content.startswith("```"):
        parts = corrected_content.split("```")
        if len(parts) >= 3:
            code_block = parts[1]
            if code_block.startswith("typescript") or code_block.startswith("ts") or code_block.startswith("html"):
                code_block = code_block.split("\n", 1)[1] if "\n" in code_block else ""
            corrected_content = code_block.strip()
        else:
            corrected_content = (
                corrected_content.replace("```typescript", "")
                .replace("```ts", "")
                .replace("```html", "")
                .replace("```", "")
                .strip()
            )

    return corrected_content.strip()


def _collect_missing_modules(errors: List[str]) -> List[str]:
    """Collect unique missing module names mentioned in compilation errors."""
    missing_modules: List[str] = []
    for error in errors:
        if "Module not found" in error or "Cannot find module" in error or "Can't resolve" in error:
            module_match = __import__("re").search(r"Can't resolve '([^']+)'|Cannot find module '([^']+)'", error)
            if module_match:
                module_name = module_match.group(1) or module_match.group(2)
                if module_name and module_name not in missing_modules:
                    missing_modules.append(module_name)
    return missing_modules


def _install_missing_modules(project_root: Path, missing_modules: List[str]) -> None:
    """Attempt to install missing npm modules detected in compilation errors."""
    if not missing_modules:
        return

    print(f"  {len(missing_modules)} missing module(s) detected; attempting installation...")
    for module in missing_modules:
        try:
            print(f"    Installing {module}...")
            result = subprocess.run(
                ["npm", "install", module],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if result.returncode == 0:
                print(f"    Installed {module} successfully.")
            else:
                print(f"    Warning: failed to install {module}: {result.stderr[:200]}")
        except Exception as exc:
            print(f"    Warning: error installing {module}: {exc}")


def _group_errors_by_file(errors: List[str], project_root: Path) -> Dict[str, List[str]]:
    """Group Angular compilation errors by the existing source file they reference."""
    errors_by_file: Dict[str, List[str]] = {}

    for error in errors:
        file_path = None
        for line in error.split("\n"):
            if "src/" in line or "./src/" in line or "projects/" in line:
                import re

                match = re.search(
                    r'((?:\./)?(?:projects/[^\s:]+/)?src/[^\s:]+\.(ts|html|scss|css|sass))',
                    line,
                )
                if match:
                    potential_path = match.group(1)
                    if potential_path.startswith("./"):
                        potential_path = potential_path[2:]
                    full_path = project_root / potential_path
                    if full_path.exists():
                        file_path = potential_path
                        break

        if file_path:
            errors_by_file.setdefault(file_path, []).append(error)
        else:
            errors_by_file.setdefault("unknown", []).append(error)

    return errors_by_file



def _fix_compilation_errors(errors: List[str], project_root: Path, client) -> List[Dict]:
    """Fix compilation errors using LLM and automatic fixes."""
    if not errors:
        return []

    fixes: List[Dict] = []

    print(f"  → Analysing {len(errors)} errors for automatic fixes...")
    for index, error in enumerate(errors):
        if "Module not found" in error or "Cannot find module" in error or "Can't resolve" in error:
            print(f"    Error {index + 1}: Missing module error detected")
            print(f"      First lines: {error.split(chr(10))[0][:150]}...")

            module_match = __import__("re").search(
                r"Can't resolve '([^']+)'|Cannot find module '([^']+)'|Module not found.*?'([^']+)'",
                error,
            )
            file_match = __import__("re").search(
                r'(?:\./)?src/([^\s:]+\.(?:ts|html|scss|css|sass))',
                error,
            )

            if module_match:
                module_name = module_match.group(1) or module_match.group(2) or module_match.group(3)
                print(f"      Module detected: {module_name}")
            else:
                print("      ⚠️ Could not extract module name")
                module_name = None

            if file_match:
                file_path = "src/" + file_match.group(1)
                print(f"      File detected: {file_path}")
            else:
                print("      Warning: could not extract the file path.")
                file_path = None

            if module_match and file_match and module_name:
                full_path = project_root / file_path
                if full_path.exists():
                    print(f"  → Applying automatic fix for missing module: {module_name} in {file_path}")
                    try:
                        content = full_path.read_text(encoding="utf-8")
                        corrected_content = _auto_fix_missing_module(content, module_name)
                        if corrected_content != content:
                            full_path.write_text(corrected_content, encoding="utf-8")
                            fixes.append(
                                {
                                    "path": file_path,
                                    "original": content,
                                    "corrected": corrected_content,
                                }
                            )
                            print(f"    ✓ Automatic fix applied and saved to {file_path}")
                        else:
                            print(f"    Warning: no changes were detected in {file_path}.")
                    except Exception as exc:
                        print(f"    ⚠️ Error in automatic fix: {exc}")
                else:
                    print(f"    Warning: file does not exist: {full_path}")
            else:
                print("    ⚠️ Could not extract module or file from error")

    _install_missing_modules(project_root, _collect_missing_modules(errors))

    errors_by_file = _group_errors_by_file(errors, project_root)

    tracked_files = [file_path for file_path in errors_by_file.keys() if file_path != "unknown"]
    print(f"  Found errors in {len(tracked_files)} file(s).")
    if "unknown" in errors_by_file:
        print(f"  ⚠️ {len(errors_by_file['unknown'])} error(s) could not be associated with a specific file")

    for file_path, file_errors in list(errors_by_file.items())[:10]:
        if file_path == "unknown":
            print(f"  Warning: skipping {len(file_errors)} error(s) without an associated file.")
            continue

        try:
            full_path = project_root / file_path
            if not full_path.exists():
                continue

            original_content = full_path.read_text(encoding="utf-8")
            corrected_content = _request_angular_compilation_fix(
                file_path,
                original_content,
                file_errors,
                client,
            )

            if corrected_content and corrected_content != original_content.strip():
                print(f"    ✓ Fix generated for {file_path}")
                fixes.append(
                    {
                        "path": str(full_path),
                        "original": original_content,
                        "corrected": corrected_content,
                        "errors": file_errors,
                    }
                )
            else:
                print(f"    ⚠️ No valid fix generated for {file_path}")
        except Exception as exc:
            print(f"  ⚠️ Error corrigiendo {file_path}: {exc}")

    return fixes



def _auto_fix_missing_module(content: str, module_name: str) -> str:
    """Automatically fix a missing module by commenting out the import and removing its uses."""
    import re

    lines = content.split("\n")
    corrected_lines: List[str] = []
    module_short_names: List[str] = []

    import_pattern = rf'import\s+\{{([^}}]+)\}}\s+from\s+["\']{re.escape(module_name)}["\']'
    import_match = re.search(import_pattern, content)
    if import_match:
        imports_str = import_match.group(1)
        module_short_names = [name.strip() for name in imports_str.split(",")]
        print(f"      → Modules detected in import: {module_short_names}")
    else:
        print(f"      ⚠️ Import for {module_name} not found")

    import_commented = False
    imports_removed = False

    for line in lines:
        if module_name in line and "import" in line and "from" in line:
            if not line.strip().startswith("//"):
                indent = len(line) - len(line.lstrip())
                corrected_lines.append(" " * indent + f"// {line.strip()} // Module not available: {module_name}")
                import_commented = True
                print(f"      → Import comentado: {line.strip()[:60]}...")
            else:
                corrected_lines.append(line)
        elif module_short_names and any(name in line for name in module_short_names):
            if "imports:" in line or ("imports" in line and "[" in line):
                original_line_for_log = line
                for module_short_name in module_short_names:
                    if module_short_name in line:
                        line = re.sub(rf',\s*{re.escape(module_short_name)}\s*,', ',', line)
                        line = re.sub(rf',\s*{re.escape(module_short_name)}\s*\]', ']', line)
                        line = re.sub(rf'\[\s*{re.escape(module_short_name)}\s*,', '[', line)
                        line = re.sub(rf'\[\s*{re.escape(module_short_name)}\s*\]', '[]', line)
                        line = re.sub(r',\s*,', ',', line)
                        line = re.sub(r',\s+', ', ', line)
                if line != original_line_for_log:
                    imports_removed = True
                    print(f"      → Module removed from imports array: {original_line_for_log.strip()[:60]}...")
                corrected_lines.append(line)
            else:
                corrected_lines.append(line)
        else:
            corrected_lines.append(line)

    if not import_commented:
        print("      ⚠️ No import was commented out")
    if not imports_removed:
        print("      ⚠️ No module was removed from the imports array")

    return "\n".join(corrected_lines)



def _apply_compilation_fixes(fixes: List[Dict], project_root: Path) -> None:
    """Apply compilation fixes generated by the repair step."""
    for fix in fixes:
        try:
            target_path = Path(fix["path"])
            target_path.write_text(fix["corrected"], encoding="utf-8")
        except Exception as exc:
            print(f"  ⚠️ Error applying fix to {fix['path']}: {exc}")



def _start_angular_dev_server(project_root: Path, port: int = 4200, wait_for_ready: bool = False):
    """Start the Angular development server on the requested port."""
    import socket

    def is_port_available(port_num: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("localhost", port_num))
                return True
            except OSError:
                return False

    if not is_port_available(port):
        print(f"  ⚠️ Port {port} is in use.")
        response = input("  Do you want to use a different port? (y/n): ")
        if response.lower() == "y":
            for candidate_port in range(4201, 4210):
                if is_port_available(candidate_port):
                    port = candidate_port
                    print(f"  Using port {port}")
                    break
            else:
                print("  ⚠️ No available port found. Using default port.")
                port = 4200
        else:
            print("  Trying port 4200 anyway...")

    def command_exists(cmd: List[str]) -> bool:
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
                check=False,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        except Exception:
            return False

    def start_background_serve(command: List[str]):
        return subprocess.Popen(
            command,
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def run_foreground_serve(command: List[str]) -> None:
        print("  Press Ctrl+C to stop the server.")
        subprocess.run(
            command,
            cwd=str(project_root),
            check=False,
        )

    def try_serve_command(
        availability_command: List[str],
        label: str,
        background_command: List[str],
        foreground_command: List[str],
    ):
        if not command_exists(availability_command):
            return False, None

        process = None
        try:
            print(f"  Starting server with '{label} --port {port}'...")
            if wait_for_ready:
                process = start_background_serve(background_command)
                return True, process
            run_foreground_serve(foreground_command)
            return True, None
        except KeyboardInterrupt:
            if process:
                process.terminate()
            print("\n  Server stopped by the user.")
            return True, None
        except Exception as exc:
            print(f"  ⚠️ Error con '{label}': {exc}")
            return False, None

    try:
        scripts = _load_package_scripts(project_root)
        if "start" in scripts and command_exists(["npm", "--version"]):
            print(f"  Starting server with 'npm start' on port {port}'...")
            print("  Press Ctrl+C to stop the server.")
            try:
                subprocess.run(
                    ["npm", "start", "--", "--port", str(port)],
                    cwd=str(project_root),
                    check=False,
                )
                return None
            except KeyboardInterrupt:
                print("\n  Server stopped by the user.")
                return None
            except Exception as exc:
                print(f"  ⚠️ Error con 'npm start': {exc}")
    except Exception:
        pass

    handled, process = try_serve_command(
        ["ng", "version"],
        "ng serve",
        ["ng", "serve", "--port", str(port)],
        ["ng", "serve", "--port", str(port), "--open"],
    )
    if handled:
        return process

    handled, process = try_serve_command(
        ["npx", "--version"],
        "npx ng serve",
        ["npx", "-y", "@angular/cli", "serve", "--port", str(port)],
        ["npx", "-y", "@angular/cli", "serve", "--port", str(port), "--open"],
    )
    if handled:
        return process

    ng_cmd_path = _resolve_node_modules_ng_command(project_root)

    if ng_cmd_path and Path(ng_cmd_path).exists():
        try:
            print(f"  Starting server with '{ng_cmd_path} serve --port {port} --open'...")
            run_foreground_serve([ng_cmd_path, "serve", "--port", str(port), "--open"])
            return None
        except KeyboardInterrupt:
            print("\n  Server stopped by the user.")
            return None
        except Exception as exc:
            print(f"  Warning: failed to run ng from node_modules: {exc}")

    print("  ⚠️ Could not start the server (ng not found in any location)")
    print(f"  You can start it manually with: ng serve --port {port}")
