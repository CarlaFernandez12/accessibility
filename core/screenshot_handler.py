"""
Módulo para capturas de pantalla automáticas durante el análisis de accesibilidad.
Toma capturas en diferentes tamaños de pantalla para verificar el diseño responsive.
"""

from pathlib import Path
from typing import List, Dict, Optional
from selenium.webdriver.remote.webdriver import WebDriver


# Tamaños de viewport comunes para testing responsive
VIEWPORT_SIZES = [
    {"name": "mobile", "width": 375, "height": 667},  # iPhone SE
    {"name": "tablet", "width": 768, "height": 1024},  # iPad
    {"name": "desktop", "width": 1920, "height": 1080},  # Full HD
]


def take_screenshots(
    driver: WebDriver,
    url: str,
    output_dir: Path,
    viewport_sizes: Optional[List[Dict[str, int]]] = None,
    prefix: str = "screenshot"
) -> List[str]:
    """
    Toma capturas de pantalla de una URL en diferentes tamaños de viewport.
    
    Args:
        driver: WebDriver de Selenium
        url: URL de la que tomar capturas
        output_dir: Directorio donde guardar las capturas
        viewport_sizes: Lista de tamaños de viewport. Si es None, usa VIEWPORT_SIZES por defecto
        prefix: Prefijo para los nombres de archivo (default: "screenshot")
    
    Returns:
        Lista de rutas a las capturas tomadas
    """
    if viewport_sizes is None:
        viewport_sizes = VIEWPORT_SIZES
    
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_paths = []
    
    try:
        # Navegar a la URL
        driver.get(url)
        
        # Esperar a que cargue la página
        import time
        time.sleep(3)
        
        # Tomar capturas en cada tamaño de viewport
        for viewport in viewport_sizes:
            width = viewport["width"]
            height = viewport["height"]
            name = viewport.get("name", f"{width}x{height}")
            
            # Cambiar tamaño de ventana
            driver.set_window_size(width, height)
            time.sleep(1)  # Esperar a que se ajuste el layout
            
            # Tomar captura
            screenshot_path = output_dir / f"{prefix}_{name}.png"
            driver.save_screenshot(str(screenshot_path))
            screenshot_paths.append(str(screenshot_path))
            print(f"  ✓ Captura guardada: {screenshot_path.name} ({width}x{height})")
        
        # Restaurar tamaño original (desktop por defecto)
        driver.set_window_size(1920, 1080)
        
    except Exception as e:
        print(f"  ⚠️ Error al tomar capturas de pantalla: {e}")
    
    return screenshot_paths


def take_component_screenshot(
    driver: WebDriver,
    element_selector: str,
    output_path: Path,
    viewport_size: Optional[Dict[str, int]] = None
) -> Optional[str]:
    """
    Toma una captura de pantalla de un elemento específico del DOM.
    
    Args:
        driver: WebDriver de Selenium
        element_selector: Selector CSS o XPath del elemento
        output_path: Ruta donde guardar la captura
        viewport_size: Tamaño del viewport. Si es None, usa el tamaño actual
    
    Returns:
        Ruta a la captura si fue exitosa, None en caso contrario
    """
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        # Si se especifica viewport, ajustarlo
        if viewport_size:
            driver.set_window_size(viewport_size["width"], viewport_size["height"])
            import time
            time.sleep(1)
        
        # Encontrar el elemento
        try:
            # Intentar primero con CSS selector
            element = driver.find_element(By.CSS_SELECTOR, element_selector)
        except:
            try:
                # Intentar con XPath
                element = driver.find_element(By.XPATH, element_selector)
            except:
                print(f"  ⚠️ No se pudo encontrar el elemento: {element_selector}")
                return None
        
        # Tomar captura del elemento
        output_path.parent.mkdir(parents=True, exist_ok=True)
        element.screenshot(str(output_path))
        return str(output_path)
        
    except Exception as e:
        print(f"  ⚠️ Error al tomar captura del elemento: {e}")
        return None


def create_screenshot_summary(screenshot_paths: List[str], output_path: Path) -> str:
    """
    Crea un resumen HTML con las capturas para fácil visualización.
    
    Args:
        screenshot_paths: Lista de rutas a las capturas
        output_path: Ruta donde guardar el HTML
    
    Returns:
        Ruta al archivo HTML generado
    """
    html_content = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Capturas de Pantalla - Análisis de Accesibilidad</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f5f5;
        }
        h1 {
            color: #333;
            border-bottom: 3px solid #007bff;
            padding-bottom: 10px;
        }
        .screenshot-container {
            background: white;
            margin: 20px 0;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .screenshot-container h2 {
            color: #555;
            margin-top: 0;
        }
        .screenshot-container img {
            max-width: 100%;
            height: auto;
            border: 1px solid #ddd;
            border-radius: 4px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
    </style>
</head>
<body>
    <h1>Capturas de Pantalla - Análisis de Accesibilidad</h1>
"""
    
    for i, path in enumerate(screenshot_paths, 1):
        path_obj = Path(path)
        relative_path = path_obj.name
        viewport_name = path_obj.stem.replace("screenshot_", "").replace("_", " ").title()
        
        html_content += f"""
    <div class="screenshot-container">
        <h2>Vista: {viewport_name}</h2>
        <img src="{relative_path}" alt="Captura de pantalla {viewport_name}">
    </div>
"""
    
    html_content += """
</body>
</html>
"""
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")
    return str(output_path)

