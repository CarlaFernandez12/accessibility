"""
Screenshot utilities used during accessibility analysis.

This module captures screenshots at multiple viewport sizes to validate
responsive behaviour, and can also create simple HTML galleries for review.
"""

from pathlib import Path
from typing import Dict, List, Optional

from selenium.webdriver.remote.webdriver import WebDriver


# Common viewport sizes for basic responsive testing
VIEWPORT_SIZES: List[Dict[str, int]] = [
    {"name": "mobile", "width": 375, "height": 667},  # iPhone SE
    {"name": "tablet", "width": 768, "height": 1024},  # iPad
    {"name": "desktop", "width": 1920, "height": 1080},  # Full HD
]


def take_screenshots(
    driver: WebDriver,
    url: str,
    output_dir: Path,
    viewport_sizes: Optional[List[Dict[str, int]]] = None,
    prefix: str = "screenshot",
) -> List[str]:
    """
    Capture full‑page screenshots of a URL for several viewport sizes.

    Args:
        driver: Selenium WebDriver instance.
        url: Target URL to capture.
        output_dir: Directory where screenshots will be written.
        viewport_sizes: Optional list of viewport dictionaries; if None, uses
                        the default VIEWPORT_SIZES.
        prefix: File name prefix for generated screenshots.

    Returns:
        List of absolute screenshot file paths.
    """
    if viewport_sizes is None:
        viewport_sizes = VIEWPORT_SIZES

    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_paths: List[str] = []

    try:
        # Navigate to the URL
        driver.get(url)

        # Give the page some time to fully render
        import time

        time.sleep(3)

        # Capture screenshots for each viewport
        for viewport in viewport_sizes:
            width = viewport["width"]
            height = viewport["height"]
            name = viewport.get("name", f"{width}x{height}")

            # Adjust window size
            driver.set_window_size(width, height)
            time.sleep(1)  # Allow layout to adjust

            # Capture screenshot
            screenshot_path = output_dir / f"{prefix}_{name}.png"
            driver.save_screenshot(str(screenshot_path))
            screenshot_paths.append(str(screenshot_path))
            print(f"  ✓ Screenshot saved: {screenshot_path.name} ({width}x{height})")

        # Restore a default desktop viewport
        driver.set_window_size(1920, 1080)

    except Exception as exc:
        print(f"  ⚠️ Error while taking screenshots: {exc}")

    return screenshot_paths


def take_component_screenshot(
    driver: WebDriver,
    element_selector: str,
    output_path: Path,
    viewport_size: Optional[Dict[str, int]] = None,
) -> Optional[str]:
    """
    Capture a screenshot of a specific DOM element.

    Args:
        driver: Selenium WebDriver instance.
        element_selector: CSS selector or XPath for the element.
        output_path: Destination path for the screenshot file.
        viewport_size: Optional viewport size to apply before capturing.

    Returns:
        Screenshot file path if successful, None otherwise.
    """
    try:
        from selenium.webdriver.common.by import By

        # Optionally adjust viewport size
        if viewport_size:
            driver.set_window_size(viewport_size["width"], viewport_size["height"])
            import time

            time.sleep(1)

        # Attempt CSS selector first
        try:
            element = driver.find_element(By.CSS_SELECTOR, element_selector)
        except Exception:
            try:
                # Fallback to XPath
                element = driver.find_element(By.XPATH, element_selector)
            except Exception:
                print(f"  ⚠️ Could not find element: {element_selector}")
                return None

        # Capture element screenshot
        output_path.parent.mkdir(parents=True, exist_ok=True)
        element.screenshot(str(output_path))
        return str(output_path)

    except Exception as exc:
        print(f"  ⚠️ Error while taking element screenshot: {exc}")
        return None


def create_screenshot_summary(screenshot_paths: List[str], output_path: Path) -> str:
    """
    Generate a simple HTML gallery summarising the provided screenshots.

    Args:
        screenshot_paths: List of screenshot file paths.
        output_path: Destination HTML path.

    Returns:
        The path to the generated HTML file.
    """
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Accessibility Analysis Screenshots</title>
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
    <h1>Accessibility Analysis Screenshots</h1>
"""

    for path in screenshot_paths:
        path_obj = Path(path)
        relative_path = path_obj.name
        viewport_name = (
            path_obj.stem.replace("screenshot_", "").replace("_", " ").title()
        )

        html_content += f"""
    <div class="screenshot-container">
        <h2>View: {viewport_name}</h2>
        <img src="{relative_path}" alt="Screenshot {viewport_name}">
    </div>
"""

    html_content += """
</body>
</html>
"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")
    return str(output_path)

