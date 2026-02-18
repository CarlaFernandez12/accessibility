import time
from typing import Any, Dict, List

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver


class DynamicContentHandler:
    """
    Helper for basic, conservative interactions with dynamic content.

    This class encapsulates small, targeted interactions (accepting cookies,
    closing modal popups, triggering lazy loading) that improve the quality
    of accessibility scans without attempting to fully automate the page.
    """

    def __init__(self, driver: WebDriver, wait_timeout: int = 5) -> None:
        self.driver = driver
        self.wait_timeout = wait_timeout

    def handle_common_interactions(self) -> Dict[str, Any]:
        """
        Perform a small set of common safe interactions.

        Returns a log structure describing what was attempted and whether
        it appears to have succeeded.
        """
        interactions_log: Dict[str, Any] = {
            "cookies_accepted": False,
            "modals_closed": False,
            "menus_expanded": False,
            "lazy_content_loaded": False,
            "errors": [],
        }

        try:
            interactions_log["cookies_accepted"] = self._accept_cookies()
            interactions_log["modals_closed"] = self._close_modals()
            interactions_log["lazy_content_loaded"] = self._load_lazy_content()

            time.sleep(2)

        except Exception as exc:
            interactions_log["errors"].append(str(exc))

        return interactions_log

    def _accept_cookies(self) -> bool:
        """Attempt to accept cookie banners using a small set of generic selectors."""
        cookie_selectors: List[str] = [
            "button[class*='cookie']",
            "button[class*='accept']",
            ".cookie-accept",
            ".accept-cookies",
        ]

        for selector in cookie_selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    if element.is_displayed():
                        element.click()
                        time.sleep(0.5)
                        return True
            except Exception:
                continue

        return False

    def _close_modals(self) -> bool:
        """Close basic modal dialogs using generic close button selectors."""
        modal_selectors: List[str] = [
            "button[class*='close']",
            ".modal-close",
            ".popup-close",
        ]

        closed_count = 0
        for selector in modal_selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    if element.is_displayed():
                        element.click()
                        closed_count += 1
                        time.sleep(0.5)
            except Exception:
                continue

        return closed_count > 0

    def _load_lazy_content(self) -> bool:
        """Trigger lazy‑loaded content using a simple scroll pattern."""
        try:
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight/2);"
            )
            time.sleep(1)
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(1)
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(1)
            return True
        except Exception:
            return False

    def execute_custom_interactions(self, interactions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Execute a caller‑provided list of scripted interactions.

        Each interaction is a small JSON‑like structure with keys such as:
        - type: "click" | "scroll" | "type" | "wait"
        - selector: CSS selector (required for all but "wait")
        - text: text to send when type == "type"
        - wait_after: seconds to wait after the interaction
        """
        results: Dict[str, Any] = {
            "successful": [],
            "failed": [],
            "total": len(interactions),
        }

        for interaction in interactions:
            try:
                interaction_type = interaction.get("type", "click")
                selector = interaction.get("selector")
                wait_after = interaction.get("wait_after", 1)

                if not selector and interaction_type != "wait":
                    continue

                element = None
                if selector:
                    element = self.driver.find_element(By.CSS_SELECTOR, selector)

                if interaction_type == "click" and element is not None:
                    element.click()
                elif interaction_type == "scroll" and element is not None:
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView(true);", element
                    )
                elif interaction_type == "type" and element is not None:
                    text = interaction.get("text", "")
                    element.clear()
                    element.send_keys(text)
                elif interaction_type == "wait":
                    time.sleep(wait_after)

                time.sleep(wait_after)
                results["successful"].append(interaction)

            except Exception as exc:
                results["failed"].append({**interaction, "error": str(exc)})

        return results
