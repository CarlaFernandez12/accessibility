"""
Módulo para análisis de accesibilidad web usando Axe Core.

Este módulo proporciona funciones para ejecutar análisis de accesibilidad
en páginas web, incluyendo soporte para contenido dinámico y múltiples estados.
"""

import time
from pathlib import Path

import requests
from selenium.webdriver.common.by import By

from config.constants import AXE_SCRIPT_URL
from core.dynamic_handler import DynamicContentHandler

# Constantes
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 5
PAGE_LOAD_WAIT_TIME = 5
SSL_WARNING_WAIT_TIME = 3

def _handle_ssl_warning(driver, target):
    """
    Maneja advertencias SSL intentando continuar automáticamente.
    
    Args:
        driver: WebDriver de Selenium
        target: URL objetivo
    """
    try:
        page_title = driver.title.lower()
        page_source = driver.page_source.lower()
        ssl_indicators = (
            "privacidad" in page_title or
            "privacy" in page_title or
            "certificado" in page_source or
            "certificate" in page_source or
            "no es privada" in page_source or
            "not private" in page_source
        )
        
        if not ssl_indicators:
            return
        
        print("  ⚠️ Detectada página de advertencia SSL, intentando continuar...")
        
        strategies = [
            lambda: driver.find_element(By.ID, "proceed-link").click(),
            lambda: _click_advanced_then_proceed(driver),
            lambda: _click_proceed_link_by_text(driver),
            lambda: driver.execute_script("window.location.href = arguments[0];", target),
        ]
        
        for strategy in strategies:
            try:
                strategy()
                time.sleep(SSL_WARNING_WAIT_TIME)
                return
            except Exception:
                continue
                
    except Exception as e:
        print(f"  ⚠️ No se pudo manejar automáticamente la advertencia SSL: {e}")


def _click_advanced_then_proceed(driver):
    """Hace clic en el botón Avanzado y luego en el enlace para continuar."""
    advanced = driver.find_element(
        By.XPATH,
        "//button[contains(text(), 'Avanzado') or contains(text(), 'Advanced')]"
    )
    advanced.click()
    time.sleep(2)
    proceed = driver.find_element(
        By.XPATH,
        "//a[contains(@id, 'proceed') or contains(@href, 'proceed')]"
    )
    proceed.click()


def _click_proceed_link_by_text(driver):
    """Busca y hace clic en un enlace que contenga 'continuar' o 'proceed'."""
    proceed = driver.find_element(
        By.XPATH,
        "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continuar') or "
        "contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'proceed')]"
    )
    proceed.click()


def _handle_navigation_ssl_warning(driver):
    """Maneja advertencias SSL durante la navegación inicial."""
    try:
        advanced_buttons = driver.find_elements(
            By.XPATH,
            "//button[contains(text(), 'Avanzado') or contains(text(), 'Advanced')]"
        )
        proceed_buttons = driver.find_elements(
            By.XPATH,
            "//a[contains(text(), 'Continuar') or contains(text(), 'Proceed') or contains(text(), 'Ir a')]"
        )
        
        if advanced_buttons:
            advanced_buttons[0].click()
            time.sleep(2)
            proceed_links = driver.find_elements(
                By.XPATH,
                "//a[contains(@id, 'proceed-link') or contains(@href, 'proceed')]"
            )
            if proceed_links:
                proceed_links[0].click()
                time.sleep(2)
        elif proceed_buttons:
            proceed_buttons[0].click()
            time.sleep(2)
    except Exception:
        pass


def _execute_axe_analysis(driver):
    """
    Ejecuta el script de Axe y retorna los resultados.
    
    Args:
        driver: WebDriver de Selenium
        
    Returns:
        Resultados del análisis de Axe
    """
    axe_script = requests.get(AXE_SCRIPT_URL).text
    driver.execute_script(axe_script)

    return driver.execute_async_script(
        "const callback = arguments[arguments.length - 1];"
        "axe.run({ runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa', 'reflow', 'language', 'navigation', 'contrast', 'keyboard', 'focus', 'text-spacing', 'viewport', 'zoom'] } })"
        ".then(results => callback(results))"
        ".catch(err => callback({ error: err.toString() }));"
    )


def run_axe_analysis(driver, url, is_local_file=False, enable_dynamic_interactions=True, custom_interactions=None):
    """
    Ejecuta análisis de accesibilidad con soporte para contenido dinámico.
    
    Args:
        driver: WebDriver de Selenium
        url: URL a analizar
        is_local_file: Si es un archivo local
        enable_dynamic_interactions: Si habilitar interacciones automáticas (por defecto True)
        custom_interactions: Lista de interacciones personalizadas
        
    Returns:
        Resultados del análisis de Axe
        
    Raises:
        Exception: Si no se pudo completar el análisis después de múltiples intentos
    """
    retry_delay = INITIAL_RETRY_DELAY

    for attempt in range(MAX_RETRIES):
        try:
            target = Path(url).resolve().as_uri() if is_local_file else url
            print(f"Analizando con Axe: {target}")
            
            try:
                driver.get(target)
            except Exception as nav_error:
                print(f"  ⚠️ Advertencia de navegación (posible SSL): {nav_error}")
                _handle_navigation_ssl_warning(driver)
            
            time.sleep(PAGE_LOAD_WAIT_TIME)
            _handle_ssl_warning(driver, target)

            if enable_dynamic_interactions and not is_local_file:
                _handle_dynamic_interactions(driver, custom_interactions)

            _wait_for_page_load(driver)
            return _execute_axe_analysis(driver)
        except Exception as e:
            print(f"Intento {attempt + 1} fallido: {e}")
            if attempt < MAX_RETRIES - 1:
                print(f"Reintentando en {retry_delay} segundos...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise Exception(
                    f"No se pudo completar el análisis después de {MAX_RETRIES} intentos"
                )


def _handle_dynamic_interactions(driver, custom_interactions):
    """Maneja interacciones dinámicas con el contenido de la página."""
    try:
        dynamic_handler = DynamicContentHandler(driver)
        dynamic_handler.handle_common_interactions()
        
        if custom_interactions:
            custom_results = dynamic_handler.execute_custom_interactions(
                custom_interactions
            )
            print(
                f"Interacciones personalizadas: "
                f"{len(custom_results['successful'])} exitosas, "
                f"{len(custom_results['failed'])} fallidas"
            )
    except Exception as e:
        print(f"Advertencia: Error en interacciones dinámicas: {e}")


def _wait_for_page_load(driver):
    """Espera a que la página termine de cargar completamente."""
    ready_state = driver.execute_script("return document.readyState")
    if ready_state != "complete":
        print("Esperando a que la página termine de cargar...")
        time.sleep(PAGE_LOAD_WAIT_TIME)


def run_axe_analysis_multiple_states(driver, url, states_config):
    """
    Ejecuta análisis de accesibilidad en múltiples estados de la misma página.
    
    Args:
        driver: WebDriver de Selenium
        url: URL a analizar
        states_config: Lista de configuraciones de estados
        
    Returns:
        Lista de resultados del análisis para cada estado
    """
    results = []
    dynamic_handler = DynamicContentHandler(driver)
    
    print(f"🔄 Iniciando análisis multi-estado para: {url}")
    
    driver.get(url)
    time.sleep(PAGE_LOAD_WAIT_TIME)
    
    for i, state_config in enumerate(states_config, 1):
        state_name = state_config.get('name', f'Estado {i}')
        print(f"\n--- Analizando Estado {i}: {state_name} ---")
        
        try:
            if state_config.get('interactions'):
                interaction_results = dynamic_handler.execute_custom_interactions(
                    state_config['interactions']
                )
                print(
                    f"Interacciones ejecutadas: "
                    f"{len(interaction_results['successful'])} exitosas"
                )

            axe_results = run_axe_analysis(
                driver, url, enable_dynamic_interactions=False
            )
            
            axe_results['state_info'] = {
                'name': state_name,
                'description': state_config.get('description', ''),
                'interactions_applied': state_config.get('interactions', []),
                'timestamp': time.time()
            }
            
            results.append(axe_results)
            print(f"✅ Estado '{state_name}' analizado exitosamente")
            
        except Exception as e:
            print(f"❌ Error analizando estado '{state_name}': {e}")
            results.append({
                'error': str(e),
                'state_info': {
                    'name': state_name,
                    'description': state_config.get('description', ''),
                    'timestamp': time.time()
                }
            })
    
    return results
