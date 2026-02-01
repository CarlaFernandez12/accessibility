import argparse
import http.server
import json
import os
import socketserver
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from openai import OpenAI

from config.constants import BASE_RESULTS_DIR
from core.analyzer import run_axe_analysis, run_axe_analysis_multiple_states
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
from utils.io_utils import (
    clear_openai_logs,
    get_image_as_base64,
    load_cache,
    save_cache,
    save_openai_logs,
    setup_directories,
)

load_dotenv()

# Constantes
DEFAULT_SERVER_PORT = 8000
MAX_SERVER_PORT = 8050
PREVIEW_PROMPT = "\n¿Deseas previsualizar la página corregida en el navegador? (y/n): "


def _create_argument_parser():
    """Crea y configura el parser de argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(
        description="Analizador y mejorador de accesibilidad web con IA."
    )
    parser.add_argument(
        "--url",
        type=str,
        help="La URL de la página a analizar."
    )
    parser.add_argument(
        "--project-path",
        type=str,
        help="Ruta a un proyecto local (Angular u otros) para mejorar accesibilidad."
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Tu clave de API de OpenAI."
    )
    parser.add_argument(
        "--disable-dynamic",
        action="store_true",
        help="Deshabilitar interacciones automáticas con contenido dinámico."
    )
    parser.add_argument(
        "--interactions-file",
        type=str,
        help="Archivo JSON con interacciones personalizadas."
    )
    parser.add_argument(
        "--multi-state-file",
        type=str,
        help="Archivo JSON con configuración de múltiples estados."
    )
    parser.add_argument(
        "--serve-app",
        action="store_true",
        help="Iniciar servidor de desarrollo Angular después de aplicar correcciones."
    )
    parser.add_argument(
        "--angular-axe",
        action="store_true",
        help="Usar también Axe sobre http://localhost:4200/ para guiar correcciones adicionales en templates Angular."
    )
    parser.add_argument(
        "--angular-axe-only",
        action="store_true",
        help="Ejecutar SOLO el flujo avanzado Angular + Axe (sin el flujo clásico de process_angular_project)."
    )
    parser.add_argument(
        "--angular-url",
        type=str,
        default="http://localhost:4200/",
        help="URL de la aplicación Angular a analizar con Axe (por defecto: http://localhost:4200/)."
    )
    parser.add_argument(
        "--react-axe",
        action="store_true",
        help="Usar Axe sobre http://localhost:3000/ para guiar correcciones en componentes React."
    )
    parser.add_argument(
        "--react-axe-only",
        action="store_true",
        help="Ejecutar SOLO el flujo avanzado React + Axe (sin análisis Angular)."
    )
    parser.add_argument(
        "--react-url",
        type=str,
        default="http://localhost:3000/",
        help="URL de la aplicación React a analizar con Axe (por defecto: http://localhost:3000/)."
    )
    return parser


def _validate_arguments(args, parser):
    """Valida los argumentos proporcionados."""
    if not args.url and not args.project_path:
        parser.error("Debes proporcionar --url o --project-path.")

    if args.url and args.project_path:
        parser.error("Selecciona solo uno de los modos: --url o --project-path.")


def _get_api_key(args):
    """Obtiene la clave de API de OpenAI desde argumentos o variables de entorno."""
    api_key = args.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("La clave de API de OpenAI no se ha proporcionado.")
    return api_key


def _sanitize_name(name):
    """Sanitiza un nombre para usarlo como nombre de directorio."""
    return "".join(c if c.isalnum() else '_' for c in name)


def _create_run_path(base_name, timestamp):
    """Crea la ruta donde se guardarán los resultados."""
    sanitized_name = _sanitize_name(base_name)
    return os.path.join(BASE_RESULTS_DIR, sanitized_name, timestamp)


def _load_json_file(file_path, error_message_prefix):
    """Carga un archivo JSON y maneja errores."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"{error_message_prefix}: {e}")
        return None


def main():
    """Función principal del programa."""
    parser = _create_argument_parser()
    args = parser.parse_args()
    _validate_arguments(args, parser)

    try:
        api_key = _get_api_key(args)
    except ValueError as e:
        print(f"Error: {e}")
        return

    client = OpenAI(api_key=api_key)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    if args.project_path:
        project_path = os.path.abspath(args.project_path)

        # Decidir si es proyecto React o Angular
        if detect_react_project(project_path) or getattr(args, "react_axe", False) or getattr(args, "react_axe_only", False):
            print(f"[Detección] ✓ Proyecto React detectado en: {project_path}")
            _process_react_project_flow(args, client, timestamp)
        else:
            _process_angular_project(args, client, timestamp)
        return

    # Sin project-path: modo páginas web
    _process_web_url(args, client, timestamp)


def _detect_react_dev_server_port(project_path: str) -> Optional[int]:
    """
    Detecta automáticamente el puerto en el que está corriendo el servidor de desarrollo React.
    
    Estrategias:
    1. Buscar en package.json (scripts de start/dev)
    2. Probar puertos comunes (3000, 5173, 8080, 3001, etc.)
    3. Verificar que responda un servidor de desarrollo
    """
    import socket
    import re
    from urllib.request import urlopen, Request
    from urllib.error import URLError
    
    project_root = Path(project_path)
    package_json = project_root / "package.json"
    
    print("[React + Axe] 🔍 Detectando puerto del servidor de desarrollo...")
    
    # Estrategia 0: Buscar en vite.config.js/ts (Vite por defecto usa 5173)
    vite_configs = [
        project_root / "vite.config.js",
        project_root / "vite.config.ts",
        project_root / "vite.config.mjs",
    ]
    for vite_config in vite_configs:
        if vite_config.exists():
            try:
                content = vite_config.read_text(encoding='utf-8')
                # Buscar server.port o port en la configuración
                port_match = re.search(r'server\s*:\s*\{[^}]*port\s*:\s*(\d+)', content, re.DOTALL)
                if not port_match:
                    port_match = re.search(r'port\s*:\s*(\d+)', content)
                if port_match:
                    port = int(port_match.group(1))
                    print(f"  → Puerto encontrado en {vite_config.name}: {port}")
                    if _test_port(port):
                        print(f"  ✓ Puerto {port} está activo y respondiendo")
                        return port
            except Exception:
                pass
    
    # Estrategia 1: Buscar en package.json
    if package_json.exists():
        try:
            with open(package_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            scripts = data.get("scripts", {})
            # Buscar en scripts de start, dev, serve
            for script_name in ["start", "dev", "serve"]:
                script = scripts.get(script_name, "")
                if script:
                    print(f"  → Revisando script '{script_name}': {script[:100]}...")
                    
                    # Si es vite, el puerto por defecto es 5173
                    if "vite" in script.lower() and "--port" not in script and "-p" not in script:
                        print(f"  → Vite detectado (puerto por defecto: 5173)")
                        if _test_port(5173):
                            print(f"  ✓ Puerto 5173 está activo y respondiendo")
                            return 5173
                    
                    # Buscar --port, -p, PORT=, o :puerto en la URL
                    port_match = re.search(r'(?:--port|--p|-p|PORT=)\s*(\d+)', script)
                    if port_match:
                        port = int(port_match.group(1))
                        print(f"  → Puerto encontrado en script: {port}")
                        if _test_port(port):
                            print(f"  ✓ Puerto {port} está activo y respondiendo")
                            return port
                        else:
                            print(f"  ⚠️ Puerto {port} no está respondiendo")
                    
                    # Buscar URL con puerto (ej: http://localhost:5173)
                    url_match = re.search(r':(\d+)', script)
                    if url_match:
                        port = int(url_match.group(1))
                        print(f"  → Puerto encontrado en URL: {port}")
                        if _test_port(port):
                            print(f"  ✓ Puerto {port} está activo y respondiendo")
                            return port
        except Exception as e:
            print(f"  ⚠️ Error leyendo package.json: {e}")
    
    # Estrategia 2: Probar puertos comunes
    print("  → Probando puertos comunes...")
    common_ports = [3000, 5173, 8080, 3001, 5174, 8081, 5000, 4000, 4200, 3002]
    
    for port in common_ports:
        print(f"    Probando puerto {port}...", end=" ")
        if _test_port(port):
            print(f"✓ ACTIVO")
            return port
        print("✗")
    
    print("  ⚠️ No se encontró ningún servidor de desarrollo activo")
    print("")
    print("  💡 Asegúrate de que el servidor de desarrollo esté corriendo:")
    print("     - Para React/Vite: npm run dev  o  npm start")
    print("     - Para Create React App: npm start")
    print("     - Para Next.js: npm run dev")
    print("")
    print("  💡 Si el servidor está corriendo en otro puerto, especifícalo con:")
    print("     --react-url http://localhost:PUERTO/")
    return None


def _test_port(port: int) -> bool:
    """Verifica si un puerto está respondiendo como servidor de desarrollo."""
    import socket
    from urllib.request import urlopen, Request
    from urllib.error import URLError
    
    # Verificar que el puerto esté abierto
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', port))
        sock.close()
        if result != 0:
            return False
    except Exception:
        return False
    
    # Verificar que responda HTTP (servidor de desarrollo)
    try:
        url = f"http://localhost:{port}/"
        req = Request(url)
        req.add_header('User-Agent', 'Mozilla/5.0')
        response = urlopen(req, timeout=3)
        # Verificar que sea HTML (servidor de desarrollo) o que responda 200
        content_type = response.headers.get('Content-Type', '')
        status = response.status
        # Aceptar cualquier respuesta 200-299 o content-type HTML
        if (200 <= status < 300) or 'text/html' in content_type.lower():
            return True
    except URLError:
        # Error de conexión - puerto no responde
        return False
    except Exception:
        # Otro error - asumir que no es un servidor válido
        return False
    
    return False


def _process_react_project_flow(args, client, timestamp):
    """Procesa un proyecto React local usando el flujo avanzado React + Axe."""
    project_path = os.path.abspath(args.project_path)
    project_name = os.path.basename(project_path.rstrip(os.sep)) or "react_project"
    run_path = _create_run_path(project_name, timestamp)

    setup_directories(run_path)
    clear_openai_logs()

    react_axe_only = getattr(args, "react_axe_only", False)

    # Flujo avanzado React + Axe (único flujo soportado para React ahora mismo)
    if getattr(args, "react_axe", False) or react_axe_only:
        # Intentar detectar el puerto automáticamente
        detected_port = _detect_react_dev_server_port(project_path)
        
        if detected_port:
            react_url = f"http://localhost:{detected_port}/"
            print(f"\n--- Flujo avanzado: React + Axe en {react_url} (puerto {detected_port} detectado automáticamente) ---")
        else:
            # Usar el puerto especificado o el por defecto
            user_url = getattr(args, "react_url", None)
            if user_url and user_url != "http://localhost:3000/":
                react_url = user_url
                print(f"\n--- Flujo avanzado: React + Axe en {react_url} (URL especificada manualmente) ---")
            else:
                react_url = "http://localhost:3000/"
                print(f"\n--- Flujo avanzado: React + Axe en {react_url} (puerto por defecto) ---")
                print("⚠️ No se pudo detectar automáticamente el puerto.")
                print("   Asegúrate de que la app React esté levantada.")
                print("   Si tu app corre en otro puerto, usa: --react-url http://localhost:PUERTO/")

        try:
            # Ejecutar Axe UNA sola vez sobre la app React (con capturas)
            axe_results, screenshot_paths = run_axe_on_react_app(
                react_url, run_path, suffix="_before", take_screenshots_flag=True
            )

            issues_by_component = map_axe_violations_to_react_components(
                axe_results, Path(project_path)
            )

            if not issues_by_component:
                print("[React + Axe] ⚠️ No se pudieron asociar violaciones de Axe a componentes concretos.")
            else:
                print(f"[React + Axe] ✓ Se han asociado violaciones de Axe a {len(issues_by_component)} componente(s).")
                fixes = fix_react_components_with_axe_violations(
                    issues_by_component, Path(project_path), client, screenshot_paths=screenshot_paths
                )
                print(f"[React + Axe] ✓ Componentes corregidos basados en Axe: {len(fixes)}")
        except Exception as e:
            print(f"⚠️ Error en React + Axe: {e}")

    print("\n¡Proceso completado para el proyecto React!")
    save_openai_logs(run_path)

def _process_angular_project(args, client, timestamp):
    """Procesa un proyecto Angular local."""
    project_path = os.path.abspath(args.project_path)
    project_name = os.path.basename(project_path.rstrip(os.sep)) or "angular_project"
    run_path = _create_run_path(project_name, timestamp)

    setup_directories(run_path)
    clear_openai_logs()

    angular_axe_only = getattr(args, "angular_axe_only", False)

    if not angular_axe_only:
        # Flujo clásico de process_angular_project
        try:
            summary = process_angular_project(
                project_path, client, run_path, serve_app=args.serve_app
            )
            print("\n--- Resumen de procesamiento del proyecto ---")
            for line in summary:
                print(line)
        except Exception as exc:
            print(f"Error procesando el proyecto en {project_path}: {exc}")
        finally:
            print("\n--- Guardando logs de OpenAI ---")
            save_openai_logs(run_path)

    # Flujo avanzado Angular + Axe
    if getattr(args, "angular_axe", False) or angular_axe_only:
        from core.angular_handler import (
            run_axe_on_angular_app,
            map_axe_violations_to_templates,
            fix_templates_with_axe_violations,
            fix_css_with_axe,
        )

        angular_url = getattr(args, "angular_url", "http://localhost:4200/")
        print(f"\n--- Flujo avanzado: Angular + Axe sobre {angular_url} ---")
        print("Asegúrate de tener la app Angular levantada (ng serve) antes de continuar.")

        try:
            # PASO 1: Ejecutar Axe sobre la app Angular en su estado original
            axe_results_before = run_axe_on_angular_app(
                angular_url, run_path, suffix=""
            )
        except Exception as e:
            print(f"⚠️ No se pudo ejecutar Axe sobre {angular_url}: {e}")
            print("Saltando fase Angular + Axe.")
        else:
            from pathlib import Path

            def _summarize_axe(results):
                """Devuelve (num_violations, num_nodos) a partir del JSON de Axe."""
                violations = results.get("violations", []) or []
                num_violations = len(violations)
                num_nodes = sum(len(v.get("nodes", []) or []) for v in violations)
                return num_violations, num_nodes

            print("\n[Angular + Axe] Resumen inicial de violaciones detectadas por Axe:")
            before_v, before_nodes = _summarize_axe(axe_results_before)
            print(f"  - Violaciones únicas: {before_v}")
            print(f"  - Nodos afectados total: {before_nodes}")

            print("\nMapeando violaciones de Axe a templates Angular...")
            issues_by_template = map_axe_violations_to_templates(
                axe_results_before, Path(project_path)
            )

            if not issues_by_template:
                print("⚠️ No se pudieron asociar violaciones de Axe a templates concretos.")
                print("   Posibles razones:")
                print("   - No se encontraron archivos *.component.html en el proyecto")
                print("   - Los templates no contienen el HTML que Axe detectó")
                print("   - El proyecto no tiene estructura Angular estándar")
                print("   → Sugerencia: Verifica que el proyecto tenga templates en src/ o app/")
            else:
                print(
                    f"Se han asociado violaciones de Axe a {len(issues_by_template)} template(s)."
                )
                fixes = fix_templates_with_axe_violations(
                    issues_by_template, Path(project_path), client
                )
                print(
                    f"\nAngular + Axe: templates corregidos basados en Axe: {len(fixes)}"
                )

            # Nueva fase: intentar corregir contraste a nivel de CSS global (styles.scss)
            print("\n[Angular + Axe] Corrigiendo contraste a nivel de CSS global (styles.scss/styles.css)...")
            css_fixes = fix_css_with_axe(axe_results_before, Path(project_path), client)
            if css_fixes:
                print(
                    f"[Angular + Axe] ✓ Se han aplicado correcciones de contraste en {len(css_fixes)} archivo(s) de estilos."
                )
            else:
                print(
                    "[Angular + Axe] No se aplicaron correcciones de contraste a nivel de CSS global "
                    "(puede que no haya estilos globales estándar o que no se hayan detectado selectores simples)."
                )

            # PASO 2: Re-ejecutar Axe tras aplicar las correcciones en templates y CSS
            print("\n[Angular + Axe] ⚠️ IMPORTANTE: Asegúrate de que la app Angular se haya recompilado")
            print("   con los cambios aplicados antes de continuar.")
            print("   Si usas 'ng serve', debería recompilarse automáticamente.")
            input("   Presiona Enter cuando la app esté recompilada y lista...")
            
            try:
                print("\n[Angular + Axe] Re-ejecutando Axe tras aplicar correcciones...")
                axe_results_after = run_axe_on_angular_app(
                    angular_url, run_path, suffix="_after"
                )
                after_v, after_nodes = _summarize_axe(axe_results_after)
                print("\n[Angular + Axe] Comparativa de violaciones antes y después:")
                print(f"  - Antes:  {before_v} violaciones / {before_nodes} nodos afectados")
                print(f"  - Después: {after_v} violaciones / {after_nodes} nodos afectados")
                diff_v = before_v - after_v
                diff_nodes = before_nodes - after_nodes
                print(
                    f"  - Mejora neta: {max(diff_v, 0)} violaciones menos, {max(diff_nodes, 0)} nodos menos afectados"
                )
                
                # Mostrar detalles de las violaciones que persisten
                if after_v > 0:
                    print("\n[Angular + Axe] ⚠️ Violaciones que persisten después de las correcciones:")
                    after_violations = axe_results_after.get("violations", []) or []
                    for v in after_violations:
                        v_id = v.get("id", "unknown")
                        v_desc = v.get("description", "")[:80]
                        v_nodes = len(v.get("nodes", []))
                        print(f"  - {v_id}: {v_desc}... ({v_nodes} nodo(s))")
                    
                    # Comparar con las violaciones originales
                    before_violations = axe_results_before.get("violations", []) or []
                    before_ids = {v.get("id") for v in before_violations}
                    after_ids = {v.get("id") for v in after_violations}
                    
                    if before_ids == after_ids:
                        print("\n[Angular + Axe] ⚠️ Las mismas violaciones persisten (mismos IDs)")
                        print("   Posibles razones:")
                        print("   1. Las correcciones no se aplicaron al elemento correcto")
                        print("   2. Las correcciones requieren cambios en CSS (no solo templates)")
                        print("   3. Las correcciones requieren recargar/recompilar la app")
                        print("   4. El LLM no aplicó las correcciones correctamente")
                    else:
                        new_violations = after_ids - before_ids
                        if new_violations:
                            print(f"\n[Angular + Axe] ⚠️ Se introdujeron {len(new_violations)} violación(es) nueva(s): {new_violations}")
            except Exception as e:
                print(
                    f"[Angular + Axe] ⚠️ No se pudo ejecutar la segunda pasada de Axe para verificar mejoras: {e}"
                )

    print("\n¡Proceso completado para el proyecto local!")


def _process_web_url(args, client, timestamp):
    """Procesa una URL web."""
    sanitized_url = _sanitize_name(urlparse(args.url).netloc)
    run_path = _create_run_path(sanitized_url, timestamp)
    
    setup_directories(run_path)

    custom_interactions = _load_json_file(
        args.interactions_file,
        "⚠️ Error cargando interacciones personalizadas"
    )
    if custom_interactions:
        print(f"✅ Interacciones personalizadas cargadas desde: {args.interactions_file}")

    multi_state_config = _load_json_file(
        args.multi_state_file,
        "⚠️ Error cargando configuración multi-estado"
    )
    if multi_state_config:
        print(f"✅ Configuración multi-estado cargada desde: {args.multi_state_file}")

    clear_openai_logs()
    
    driver = None
    accessible_page_path = None

    try:
        driver = setup_driver()
        initial_results = _run_initial_analysis(
            driver, args, multi_state_config, custom_interactions, run_path
        )

        if not initial_results or not initial_results.get('violations'):
            print("No se encontraron violaciones de accesibilidad. ¡El trabajo ha terminado!")
            return

        accessible_page_path = _generate_accessible_page(
            driver, args.url, initial_results, client, run_path
        )

        _run_final_analysis_and_report(
            driver, initial_results, accessible_page_path, run_path
        )

        print("\n--- Guardando logs de OpenAI ---")
        save_openai_logs(run_path)
        print("\n¡Proceso completado con éxito!")

    except Exception as e:
        print(f"Ocurrió un error inesperado en el proceso: {e}")
    finally:
        if driver:
            print("Cerrando el WebDriver.")
            driver.quit()

    if accessible_page_path and os.path.exists(accessible_page_path):
        _serve_preview_if_requested(accessible_page_path)


def _run_initial_analysis(driver, args, multi_state_config, custom_interactions, run_path):
    """Ejecuta el análisis inicial de accesibilidad."""
    if multi_state_config:
        print("\n--- PASO 1: Análisis multi-estado de la página ---")
        initial_results = run_axe_analysis_multiple_states(
            driver, args.url, multi_state_config
        )
        
        for i, state_result in enumerate(initial_results):
            state_name = state_result.get('state_info', {}).get('name', f'Estado_{i+1}')
            state_file = os.path.join(run_path, f"initial_report_{state_name}.json")
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump(state_result, f, indent=4)
        
        if initial_results:
            return initial_results[0]
        else:
            print("❌ No se pudieron obtener resultados de ningún estado")
            return None
    else:
        print("\n--- PASO 1: Análisis de la página original ---")
        enable_dynamic = not args.disable_dynamic
        initial_results = run_axe_analysis(
            driver,
            args.url,
            enable_dynamic_interactions=enable_dynamic,
            custom_interactions=custom_interactions
        )
        
        with open(os.path.join(run_path, "initial_report.json"), 'w') as f:
            json.dump(initial_results, f, indent=4)
        
        return initial_results


def _generate_accessible_page(driver, url, initial_results, client, run_path):
    """Genera la página accesible corregida."""
    print("\n--- PASO 2: Generando página accesible ---")
    original_html = driver.page_source
    original_html_path = os.path.join(run_path, "original_page.html")
    
    with open(original_html_path, 'w', encoding='utf-8') as f:
        f.write(original_html)

    # TOMAR CAPTURAS DE PANTALLA ANTES DE CORRECCIONES (para mantener responsive)
    print("  → Tomando capturas de pantalla en diferentes tamaños...")
    screenshots_dir = Path(run_path) / "screenshots" / "before"
    screenshot_paths = take_screenshots(
        driver,
        url,
        screenshots_dir,
        prefix="before"
    )
    if screenshot_paths:
        print(f"  ✓ {len(screenshot_paths)} capturas guardadas")
        print(f"  → Las capturas se usarán para mantener el diseño responsive durante las correcciones")
    else:
        screenshot_paths = []  # Asegurar que es una lista vacía

    media_descriptions = process_media_elements(driver, url, client)
    
    try:
        accessible_html = generate_accessible_html_with_parser(
            original_html,
            initial_results,
            media_descriptions,
            client,
            url,
            driver,
            screenshot_paths
        )
    except Exception as e:
        print(f"  ⚠️ Error generando HTML accesible: {e}")
        print("  Intentando continuar con el HTML original...")
        accessible_html = original_html

    accessible_page_path = os.path.join(run_path, "accessible_page.html")
    with open(accessible_page_path, 'w', encoding='utf-8') as f:
        f.write(accessible_html)

    return accessible_page_path


def _run_final_analysis_and_report(driver, initial_results, accessible_page_path, run_path):
    """Ejecuta el análisis final y genera el informe comparativo."""
    print("\n--- PASO 3: Análisis de la página corregida ---")
    final_results = run_axe_analysis(driver, accessible_page_path, is_local_file=True)
    
    with open(os.path.join(run_path, "final_report.json"), 'w') as f:
        json.dump(final_results, f, indent=4)

    print("\n--- PASO 4: Generando informe comparativo ---")
    report_path = os.path.join(run_path, "comparison_report.html")
    generate_comparison_report(initial_results, final_results, report_path)


def _serve_preview_if_requested(accessible_page_path):
    """Inicia un servidor local para previsualizar la página si el usuario lo solicita."""
    serve_prompt = input(PREVIEW_PROMPT)
    if serve_prompt.lower() != 'y':
        return

    abs_path = os.path.abspath(accessible_page_path)
    base_dir = os.path.dirname(abs_path)
    file_name = os.path.basename(abs_path)
    os.chdir(base_dir)

    httpd = _find_available_port()
    if not httpd:
        print("Error: No se pudo iniciar el servidor local.")
        return

    url_to_open = f"http://localhost:{httpd.server_address[1]}/{file_name}"
    print(f"\nIniciando servidor local en: {url_to_open}")
    print("Presiona Ctrl+C para detener el servidor.")
    webbrowser.open_new_tab(url_to_open)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido por el usuario.")
    finally:
        httpd.server_close()


def _find_available_port():
    """Encuentra un puerto disponible para el servidor local."""
    Handler = http.server.SimpleHTTPRequestHandler
    
    for port in range(DEFAULT_SERVER_PORT, MAX_SERVER_PORT):
        try:
            return socketserver.TCPServer(("", port), Handler)
        except OSError:
            print(f"Puerto {port} en uso, probando el siguiente...")
    
    return None

if __name__ == "__main__":
    main()