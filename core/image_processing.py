import base64
import hashlib
import json
import os
import time
from typing import Any, Dict

import requests
from requests.exceptions import SSLError
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from urllib.parse import urljoin, urlparse

from config.constants import CACHE_DIR, IMAGE_DOMAIN_BLACKLIST
from utils.io_utils import get_image_as_base64, load_cache, log_openai_call, save_cache


def get_image_description(image_path: str, client: Any) -> str:
    """
    Ask the vision‑enabled model for a concise alt‑text description.

    The description is intended for use as HTML `alt` content and should be
    understandable and useful for screen‑reader users.
    """
    print(f"Generating description for image: {os.path.basename(image_path)}")
    base64_image = get_image_as_base64(image_path)
    if not base64_image:
        return "Image could not be processed."

    try:
        user_message = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Describe this image to generate alternative text ('alt') "
                        "for a web page. Be concise and helpful for a visually "
                        "impaired user."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    },
                },
            ],
        }

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[user_message],
            max_tokens=100,
        )

        description = response.choices[0].message.content.strip()

        log_openai_call(
            prompt=(
                "Vision API call - Describe this image to generate alternative "
                f"text ('alt') for a web page. Image: {os.path.basename(image_path)}"
            ),
            response=description,
            model="gpt-4o",
            call_type="vision",
        )

        return description
    except Exception as exc:
        print(f"Error calling OpenAI: {exc}")
        return "Description not available."


def process_media_elements(
    driver: WebDriver,
    base_url: str,
    client: Any,
) -> Dict[str, str]:
    """
    Process <img> elements on the page and generate alt‑text descriptions.

    Images are downloaded to a local cache directory, described via the
    vision model, and the results are stored in a cache so repeated runs
    do not re‑describe the same URLs.
    """
    print("Processing media elements (images)...")
    cache: Dict[str, Dict[str, str]] = load_cache()
    media_descriptions: Dict[str, str] = {}

    images = driver.find_elements(By.TAG_NAME, "img")
    print(f"Found {len(images)} images.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/109.0.0.0 Safari/537.36"
        )
    }
    max_retries = 3
    retry_delay = 3

    for img in images:
        src = img.get_attribute("src")
        if not src:
            continue

        img_url = urljoin(base_url, src)
        domain = urlparse(img_url).netloc
        if any(blocked in domain for blocked in IMAGE_DOMAIN_BLACKLIST):
            print(f"SKIP: Blacklisted domain: {img_url}")
            continue

        url_hash = hashlib.sha256(img_url.encode("utf-8")).hexdigest()
        if img_url in cache:
            print(f"CACHE HIT: {img_url}")
            media_descriptions[src] = cache[img_url]["description"]
            continue

        print(f"Downloading image: {img_url}")
        response = None
        for attempt in range(max_retries):
            try:
                # First, try with normal SSL verification
                response = requests.get(
                    img_url,
                    stream=True,
                    timeout=15,
                    headers=headers,
                    verify=True,
                )
                response.raise_for_status()
                break
            except SSLError as ssl_error:
                # For SSL verification failures, retry without verification
                print(
                    f"  > SSL error on attempt {attempt + 1}, "
                    f"retrying without SSL verification: {ssl_error}"
                )
                try:
                    response = requests.get(
                        img_url,
                        stream=True,
                        timeout=15,
                        headers=headers,
                        verify=False,
                    )
                    response.raise_for_status()
                    print("  > ✓ Image downloaded (without SSL verification)")
                    break
                except Exception as exc2:
                    print(
                        f"  > Attempt {attempt + 1} failed even without SSL "
                        f"verification: {exc2}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
            except Exception as exc:
                print(f"  > Attempt {attempt + 1} failed: {exc}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)

        if not response or not response.ok:
            continue

        ext = ".jpg"
        content_type = response.headers.get("content-type", "")
        if "image" in content_type:
            ext_part = content_type.split("/")[-1].split(";")[0].strip()
            ext = f".{ext_part}" if ext_part else ext

        file_name = f"{url_hash}{ext}"
        file_path = os.path.join(CACHE_DIR, file_name)

        try:
            with open(file_path, "wb") as file:
                for chunk in response.iter_content(8192):
                    file.write(chunk)

            description = get_image_description(file_path, client)
            media_descriptions[src] = description
            cache[img_url] = {"local_path": file_path, "description": description}
            save_cache(cache)
            print("  > Image processed and cached.")
        except Exception as exc:
            print(f"  > Error processing image {img_url}: {exc}")

    return media_descriptions

