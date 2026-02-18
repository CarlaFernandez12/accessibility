"""
Accessibility analysis helpers using axe-core.

This module centralises all interaction between Selenium and axe-core:
loading the script into the page, handling SSL warnings, supporting basic
dynamic content interactions and performing multi‑state scans.
"""

import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from config.constants import AXE_SCRIPT_URL
from core.dynamic_handler import DynamicContentHandler

# Retry and timing configuration
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 5
PAGE_LOAD_WAIT_TIME = 5
SSL_WARNING_WAIT_TIME = 3


def _handle_ssl_warning(driver: WebDriver, target: str) -> None:
    """
    Try to automatically bypass common SSL browser warning pages.
    """
    try:
        page_title = driver.title.lower()
        page_source = driver.page_source.lower()
        ssl_indicators = (
            "privacidad" in page_title
            or "privacy" in page_title
            or "certificado" in page_source
            or "certificate" in page_source
            or "no es privada" in page_source
            or "not private" in page_source
        )

        if not ssl_indicators:
            return

        print("  ⚠️ SSL warning page detected, attempting to continue...")

        strategies = [
            lambda: driver.find_element(By.ID, "proceed-link").click(),
            lambda: _click_advanced_then_proceed(driver),
            lambda: _click_proceed_link_by_text(driver),
            lambda: driver.execute_script(
                "window.location.href = arguments[0];", target
            ),
        ]

        for strategy in strategies:
            try:
                strategy()
                time.sleep(SSL_WARNING_WAIT_TIME)
                return
            except Exception:
                continue

    except Exception as exc:
        print(
            "  ⚠️ Could not automatically handle SSL warning page: "
            f"{exc}"
        )


def _click_advanced_then_proceed(driver: WebDriver) -> None:
    """Click the 'Advanced' button and then a 'proceed' link, if present."""
    advanced = driver.find_element(
        By.XPATH,
        "//button[contains(text(), 'Avanzado') or contains(text(), 'Advanced')]",
    )
    advanced.click()
    time.sleep(2)
    proceed = driver.find_element(
        By.XPATH,
        "//a[contains(@id, 'proceed') or contains(@href, 'proceed')]",
    )
    proceed.click()


def _click_proceed_link_by_text(driver: WebDriver) -> None:
    """Find and click a link that contains 'continuar' or 'proceed'."""
    proceed = driver.find_element(
        By.XPATH,
        "//a[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continuar') or "
        "contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'proceed')]",
    )
    proceed.click()


def _handle_navigation_ssl_warning(driver: WebDriver) -> None:
    """Handle SSL warnings that appear during initial navigation."""
    try:
        advanced_buttons = driver.find_elements(
            By.XPATH,
            "//button[contains(text(), 'Avanzado') or contains(text(), 'Advanced')]",
        )
        proceed_buttons = driver.find_elements(
            By.XPATH,
            "//a[contains(text(), 'Continuar') or contains(text(), 'Proceed') or contains(text(), 'Ir a')]",
        )

        if advanced_buttons:
            advanced_buttons[0].click()
            time.sleep(2)
            proceed_links = driver.find_elements(
                By.XPATH,
                "//a[contains(@id, 'proceed-link') or contains(@href, 'proceed')]",
            )
            if proceed_links:
                proceed_links[0].click()
                time.sleep(2)
        elif proceed_buttons:
            proceed_buttons[0].click()
            time.sleep(2)
    except Exception:
        # Failing to handle this automatically should not abort the analysis.
        pass


def _execute_axe_analysis(driver: WebDriver) -> Dict[str, Any]:
    """
    Inject axe-core into the page and execute an accessibility scan.

    Returns:
        The raw results object produced by axe.run(...)
    """
    axe_script = requests.get(AXE_SCRIPT_URL).text
    driver.execute_script(axe_script)

    return driver.execute_async_script(
        "const callback = arguments[arguments.length - 1];"
        "axe.run({ runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa', 'reflow', 'language', 'navigation', 'contrast', 'keyboard', 'focus', 'text-spacing', 'viewport', 'zoom'] } })"
        ".then(results => callback(results))"
        ".catch(err => callback({ error: err.toString() }));"
    )


def run_axe_analysis(
    driver: WebDriver,
    url: str,
    is_local_file: bool = False,
    enable_dynamic_interactions: bool = True,
    custom_interactions: Any = None,
) -> Dict[str, Any]:
    """
    Run an axe-core accessibility analysis, with optional dynamic interactions.

    Args:
        driver: Selenium WebDriver.
        url: URL to analyse.
        is_local_file: Whether the target is a local file path.
        enable_dynamic_interactions: If True, run basic dynamic interactions
            before executing axe (cookies, modals, simple scroll).
        custom_interactions: Optional list of caller‑defined interactions.

    Returns:
        Raw axe-core results as a dict.

    Raises:
        Exception: If all retry attempts fail.
    """
    retry_delay = INITIAL_RETRY_DELAY

    for attempt in range(MAX_RETRIES):
        try:
            target = Path(url).resolve().as_uri() if is_local_file else url
            print(f"Running Axe analysis on: {target}")

            try:
                driver.get(target)
            except Exception as nav_error:
                print(f"  ⚠️ Navigation warning (possible SSL issue): {nav_error}")
                _handle_navigation_ssl_warning(driver)

            time.sleep(PAGE_LOAD_WAIT_TIME)
            _handle_ssl_warning(driver, target)

            if enable_dynamic_interactions and not is_local_file:
                _handle_dynamic_interactions(driver, custom_interactions)

            _wait_for_page_load(driver)
            return _execute_axe_analysis(driver)
        except Exception as exc:
            print(f"Attempt {attempt + 1} failed: {exc}")
            if attempt < MAX_RETRIES - 1:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise Exception(
                    f"Could not complete analysis after {MAX_RETRIES} attempts"
                ) from exc


def _handle_dynamic_interactions(driver: WebDriver, custom_interactions: Any) -> None:
    """Execute built‑in and optional custom dynamic interactions."""
    try:
        dynamic_handler = DynamicContentHandler(driver)
        dynamic_handler.handle_common_interactions()

        if custom_interactions:
            custom_results = dynamic_handler.execute_custom_interactions(
                custom_interactions
            )
            print(
                "Custom interactions: "
                f"{len(custom_results['successful'])} successful, "
                f"{len(custom_results['failed'])} failed"
            )
    except Exception as exc:
        print(f"Warning: error during dynamic interactions: {exc}")


def _wait_for_page_load(driver: WebDriver) -> None:
    """Wait for the page readyState to become 'complete'."""
    ready_state = driver.execute_script("return document.readyState")
    if ready_state != "complete":
        print("Waiting for page to finish loading...")
        time.sleep(PAGE_LOAD_WAIT_TIME)


def run_axe_analysis_multiple_states(
    driver: WebDriver,
    url: str,
    states_config: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Run axe-core analysis on multiple interaction states of the same page.

    Each state can specify a name, description and a list of interactions
    that are executed before the axe run.
    """
    results: List[Dict[str, Any]] = []
    dynamic_handler = DynamicContentHandler(driver)

    print(f"🔄 Starting multi‑state analysis for: {url}")

    driver.get(url)
    time.sleep(PAGE_LOAD_WAIT_TIME)

    for index, state_config in enumerate(states_config, 1):
        state_name = state_config.get("name", f"State {index}")
        print(f"\n--- Analysing state {index}: {state_name} ---")

        try:
            if state_config.get("interactions"):
                interaction_results = dynamic_handler.execute_custom_interactions(
                    state_config["interactions"]
                )
                print(
                    "Interactions executed: "
                    f"{len(interaction_results['successful'])} successful"
                )

            axe_results = run_axe_analysis(
                driver,
                url,
                enable_dynamic_interactions=False,
            )

            axe_results["state_info"] = {
                "name": state_name,
                "description": state_config.get("description", ""),
                "interactions_applied": state_config.get("interactions", []),
                "timestamp": time.time(),
            }

            results.append(axe_results)
            print(f"✅ State '{state_name}' analysed successfully")

        except Exception as exc:
            print(f"❌ Error analysing state '{state_name}': {exc}")
            results.append(
                {
                    "error": str(exc),
                    "state_info": {
                        "name": state_name,
                        "description": state_config.get("description", ""),
                        "timestamp": time.time(),
                    },
                }
            )

    return results
