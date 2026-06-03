"""
Screenshot utilities used during accessibility analysis.

This module captures screenshots at multiple viewport sizes to validate
responsive behaviour.
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

