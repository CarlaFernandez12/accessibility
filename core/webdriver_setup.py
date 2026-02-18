import os
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.remote.webdriver import WebDriver
from webdriver_manager.chrome import ChromeDriverManager


def setup_driver() -> WebDriver:
    """
    Configure and create a headless Chrome WebDriver instance.

    The configuration is tuned for robustness in automated environments:
    generous timeouts, relaxed SSL handling and support for mixed content,
    while still running headless for CI‑friendly execution.
    """
    print("Configuring Chrome WebDriver...")
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--browser-timeout=300000")
    options.add_argument("--script-timeout=300000")
    options.add_argument("--page-load-timeout=300000")

    # Allow HTTP (non‑secure) sites and mixed content when necessary
    options.add_argument("--allow-running-insecure-content")
    options.add_argument("--disable-web-security")
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")
    options.add_argument("--ignore-certificate-errors-spki-list")
    options.add_argument("--allow-insecure-localhost")
    options.add_argument("--disable-features=VizDisplayCompositor")

    # Reduce noisy logging from Chrome / Selenium
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    # Preferences for mixed content and automatic HTTPS redirects
    prefs = {
        "profile.default_content_setting_values": {
            "mixed_content": 1,
        },
        "profile.content_settings.exceptions.automatic_https_redirects": {
            "*": {
                "setting": 1,
            }
        },
    }
    options.add_experimental_option("prefs", prefs)

    # Disable some automation flags that may cause warnings in the browser
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option(
        "excludeSwitches",
        ["enable-automation", "enable-logging"],
    )

    local_chromedriver_path = "chromedriver.exe"

    if os.path.exists(local_chromedriver_path):
        print(
            "Using local chromedriver found at: "
            f"{os.path.abspath(local_chromedriver_path)}"
        )
        service = ChromeService(executable_path=local_chromedriver_path)
    else:
        print("Local chromedriver not found. Using webdriver-manager to download it.")
        service = ChromeService(ChromeDriverManager().install())

    service.service_args = ["--verbose"]

    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            driver: WebDriver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(300)
            driver.set_script_timeout(300)
            driver.implicitly_wait(60)
            return driver
        except Exception as exc:
            print(f"Attempt {attempt + 1} failed: {exc}")
            if attempt < max_retries - 1:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise Exception(
                    "Could not initialize WebDriver after multiple attempts"
                ) from exc
