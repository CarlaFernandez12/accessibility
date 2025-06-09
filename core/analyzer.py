import time
import requests
from pathlib import Path
from config.constants import AXE_SCRIPT_URL

def run_axe_analysis(driver, url, is_local_file=False):
    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            if is_local_file:
                target = Path(url).resolve().as_uri()
            else:
                target = url

            print(f"Analizando con Axe: {target}")
            
            driver.get(target)
            time.sleep(5)

            ready_state = driver.execute_script("return document.readyState")
            if ready_state != "complete":
                print("Esperando a que la página termine de cargar...")
                time.sleep(5)

            axe_script = requests.get(AXE_SCRIPT_URL).text
            driver.execute_script(axe_script)

            results = driver.execute_async_script(
                "const callback = arguments[arguments.length - 1];"
                "axe.run({ runOnly: { type: 'tag', values: ['wcag2a'] } })"
                ".then(results => callback(results))"
                ".catch(err => callback({ error: err.toString() }));"
            )
            return results
        except Exception as e:
            print(f"Intento {attempt + 1} fallido: {e}")
            if attempt < max_retries - 1:
                print(f"Reintentando en {retry_delay} segundos...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise Exception(f"No se pudo completar el análisis después de {max_retries} intentos")
