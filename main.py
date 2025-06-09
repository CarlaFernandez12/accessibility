import os
import argparse
import json
from datetime import datetime
from urllib.parse import urlparse
from dotenv import load_dotenv
from openai import OpenAI
from utils.io_utils import setup_directories, load_cache, save_cache, get_image_as_base64
from core.analyzer import run_axe_analysis
from core.image_processing import process_media_elements
from core.report import generate_comparison_report
from core.html_generator import generate_accessible_html_with_parser
from core.webdriver_setup import setup_driver
from config.constants import BASE_RESULTS_DIR
import http.server
import socketserver
import webbrowser

load_dotenv()

def main():
    parser = argparse.ArgumentParser(description="Analizador y mejorador de accesibilidad web con IA.")
    parser.add_argument("--url", type=str, required=True, help="La URL de la página a analizar.")
    parser.add_argument("--api-key", type=str, default=None, help="Tu clave de API de OpenAI.")
    
    args = parser.parse_args()
    api_key = args.api_key or os.getenv("OPENAI_API_KEY")

    if not api_key:
        print("Error: La clave de API de OpenAI no se ha proporcionado.")
        return

    sanitized_url = "".join(c if c.isalnum() else '_' for c in urlparse(args.url).netloc)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_path = os.path.join(BASE_RESULTS_DIR, sanitized_url, timestamp)
    
    setup_directories(run_path)
    client = OpenAI(api_key=api_key)

    driver = None
    accessible_page_path = None

    try:
        driver = setup_driver()

        print("\n--- PASO 1: Análisis de la página original ---")
        initial_results = run_axe_analysis(driver, args.url)
        with open(os.path.join(run_path, "initial_report.json"), 'w') as f:
            json.dump(initial_results, f, indent=4)

        if not initial_results or not initial_results.get('violations'):
            print("No se encontraron violaciones de accesibilidad. ¡El trabajo ha terminado!")
            return

        print("\n--- PASO 2: Generando página accesible ---")
        original_html = driver.page_source
        media_descriptions = process_media_elements(driver, args.url, client)
        accessible_html = generate_accessible_html_with_parser(
            original_html,
            initial_results,
            media_descriptions,
            client,
            args.url,
            driver
        )

        accessible_page_path = os.path.join(run_path, "accessible_page.html")
        with open(accessible_page_path, 'w', encoding='utf-8') as f:
            f.write(accessible_html)

        print("\n--- PASO 3: Análisis de la página corregida ---")
        final_results = run_axe_analysis(driver, accessible_page_path, is_local_file=True)
        with open(os.path.join(run_path, "final_report.json"), 'w') as f:
            json.dump(final_results, f, indent=4)

        print("\n--- PASO 4: Generando informe comparativo ---")
        report_path = os.path.join(run_path, "comparison_report.html")
        generate_comparison_report(initial_results, final_results, report_path)

        print("\n¡Proceso completado con éxito!")

    except Exception as e:
        print(f"Ocurrió un error inesperado en el proceso: {e}")
    finally:
        if driver:
            print("Cerrando el WebDriver.")
            driver.quit()

    if accessible_page_path and os.path.exists(accessible_page_path):
        serve_prompt = input("\n¿Deseas previsualizar la página corregida en el navegador? (y/n): ")
        if serve_prompt.lower() == 'y':
            PORT = 8000
            abs_path = os.path.abspath(accessible_page_path)
            base_dir = os.path.dirname(abs_path)
            file_name = os.path.basename(abs_path)
            os.chdir(base_dir)

            Handler = http.server.SimpleHTTPRequestHandler
            httpd = None

            while PORT < 8050:
                try:
                    httpd = socketserver.TCPServer(("", PORT), Handler)
                    break
                except OSError:
                    print(f"Puerto {PORT} en uso, probando el siguiente...")
                    PORT += 1

            if not httpd:
                print("Error: No se pudo iniciar el servidor local.")
                return

            url_to_open = f"http://localhost:{PORT}/{file_name}"
            print(f"\nIniciando servidor local en: {url_to_open}")
            print("Presiona Ctrl+C para detener el servidor.")
            webbrowser.open_new_tab(url_to_open)

            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nServidor detenido por el usuario.")
            finally:
                httpd.server_close()

if __name__ == "__main__":
    main()
