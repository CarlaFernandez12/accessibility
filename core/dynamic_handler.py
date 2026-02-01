import time
from typing import Dict, Any
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, NoSuchElementException


class DynamicContentHandler:
    """Maneja interacciones básicas con contenido dinámico"""
    
    def __init__(self, driver, wait_timeout: int = 5):
        self.driver = driver
        self.wait_timeout = wait_timeout
        
    def handle_common_interactions(self) -> Dict[str, Any]:
        """
        Maneja interacciones básicas de forma segura
        """
        interactions_log = {
            "cookies_accepted": False,
            "modals_closed": False,
            "menus_expanded": False,
            "lazy_content_loaded": False,
            "errors": []
        }
        
        try:

            interactions_log["cookies_accepted"] = self._accept_cookies()
            
            interactions_log["modals_closed"] = self._close_modals()
            
            interactions_log["lazy_content_loaded"] = self._load_lazy_content()
            
            time.sleep(2)
            
        except Exception as e:
            interactions_log["errors"].append(str(e))
        
        return interactions_log
    
    def _accept_cookies(self) -> bool:
        """Acepta cookies de forma segura"""
        cookie_selectors = [
            "button[class*='cookie']",
            "button[class*='accept']",
            ".cookie-accept",
            ".accept-cookies"
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
        """Cierra modales básicos"""
        modal_selectors = [
            "button[class*='close']",
            ".modal-close",
            ".popup-close"
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
        """Carga contenido lazy con scroll simple"""
        try:
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
            time.sleep(1)
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
            self.driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(1)
            return True
        except Exception:
            return False
    
    def execute_custom_interactions(self, interactions):
        """Ejecuta interacciones personalizadas de forma segura"""
        results = {
            "successful": [],
            "failed": [],
            "total": len(interactions)
        }
        
        for i, interaction in enumerate(interactions):
            try:
                interaction_type = interaction.get("type", "click")
                selector = interaction.get("selector")
                wait_after = interaction.get("wait_after", 1)
                
                if not selector:
                    continue
                
                element = self.driver.find_element(By.CSS_SELECTOR, selector)
                
                if interaction_type == "click":
                    element.click()
                elif interaction_type == "scroll":
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", element)
                elif interaction_type == "type":
                    text = interaction.get("text", "")
                    element.clear()
                    element.send_keys(text)
                elif interaction_type == "wait":
                    time.sleep(wait_after)
                
                time.sleep(wait_after)
                results["successful"].append(interaction)
                
            except Exception as e:
                results["failed"].append({**interaction, "error": str(e)})
        
        return results
