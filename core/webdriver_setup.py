import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

def setup_driver():
    print("Configurando el WebDriver de Chrome...")
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--browser-timeout=300000")
    options.add_argument("--script-timeout=300000")
    options.add_argument("--page-load-timeout=300000")
    # Permitir sitios HTTP (no seguros) y contenido mixto
    options.add_argument("--allow-running-insecure-content")
    options.add_argument("--disable-web-security")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--ignore-certificate-errors-spki-list")
    options.add_argument("--allow-insecure-localhost")
    options.add_argument("--disable-features=VizDisplayCompositor")
    # Deshabilitar advertencias de seguridad para sitios HTTP
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    # Configurar preferencias para ignorar errores SSL
    prefs = {
        "profile.default_content_setting_values": {
            "mixed_content": 1,  # Permitir contenido mixto (HTTP en HTTPS)
        },
        "profile.content_settings.exceptions.automatic_https_redirects": {
            "*": {
                "setting": 1  # Permitir redirecciones automáticas HTTP->HTTPS
            }
        }
    }
    options.add_experimental_option("prefs", prefs)
    # Añadir opción experimental para ignorar errores de certificado
    options.add_experimental_option('useAutomationExtension', False)
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])

    local_chromedriver_path = "chromedriver.exe"

    if os.path.exists(local_chromedriver_path):
        print(f"Usando el chromedriver local encontrado en: {os.path.abspath(local_chromedriver_path)}")
        service = ChromeService(executable_path=local_chromedriver_path)
    else:
        print("Chromedriver local no encontrado. Usando webdriver-manager para descargarlo.")
        service = ChromeService(ChromeDriverManager().install())

    service.service_args = ['--verbose']

    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(300)
            driver.set_script_timeout(300)
            driver.implicitly_wait(60)
            return driver
        except Exception as e:
            print(f"Intento {attempt + 1} fallido: {e}")
            if attempt < max_retries - 1:
                print(f"Reintentando en {retry_delay} segundos...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise Exception("No se pudo inicializar el WebDriver después de varios intentos")
