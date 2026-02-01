import os
import time
import hashlib
import json
import base64
import requests
from requests.exceptions import SSLError
from urllib.parse import urljoin, urlparse
from selenium.webdriver.common.by import By
from config.constants import IMAGE_DOMAIN_BLACKLIST, CACHE_DIR
from utils.io_utils import load_cache, save_cache, get_image_as_base64, log_openai_call


def get_image_description(image_path, client):
    print(f"Generando descripción para la imagen: {os.path.basename(image_path)}")
    base64_image = get_image_as_base64(image_path)
    if not base64_image:
        return "No se pudo procesar la imagen."

    try:
        user_message = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Describe esta imagen para el texto alternativo ('alt') de una página web. Sé conciso y útil para una persona con discapacidad visual."
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                }
            ],
        }
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[user_message],
            max_tokens=100,
        )
        
        description = response.choices[0].message.content.strip()
        
        log_openai_call(
            prompt=f"Vision API call - Describe esta imagen para el texto alternativo ('alt') de una página web. Imagen: {os.path.basename(image_path)}",
            response=description,
            model="gpt-4o",
            call_type="vision"
        )
        
        return description
    except Exception as e:
        print(f"Error al llamar a OpenAI: {e}")
        return "Descripción no disponible."

def process_media_elements(driver, base_url, client):
    print("Procesando elementos de medios (imágenes)...")
    cache = load_cache()
    media_descriptions = {}

    images = driver.find_elements(By.TAG_NAME, "img")
    print(f"Se encontraron {len(images)} imágenes.")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36'
    }
    max_retries = 3
    retry_delay = 3

    for img in images:
        src = img.get_attribute("src")
        if not src:
            continue

        img_url = urljoin(base_url, src)
        domain = urlparse(img_url).netloc
        if any(d in domain for d in IMAGE_DOMAIN_BLACKLIST):
            print(f"SKIP: Dominio bloqueado: {img_url}")
            continue

        url_hash = hashlib.sha256(img_url.encode('utf-8')).hexdigest()
        if img_url in cache:
            print(f"CACHE HIT: {img_url}")
            media_descriptions[src] = cache[img_url]['description']
            continue

        print(f"Descargando imagen: {img_url}")
        response = None
        for attempt in range(max_retries):
            try:
                # Intentar primero con verificación SSL normal
                response = requests.get(img_url, stream=True, timeout=15, headers=headers, verify=True)
                response.raise_for_status()
                break
            except SSLError as ssl_error:
                # Si falla la verificación SSL, intentar sin verificación (para certificados autofirmados)
                print(f"  > Error SSL en intento {attempt + 1}, reintentando sin verificación SSL: {ssl_error}")
                try:
                    response = requests.get(img_url, stream=True, timeout=15, headers=headers, verify=False)
                    response.raise_for_status()
                    print(f"  > ✓ Imagen descargada (sin verificación SSL)")
                    break
                except Exception as e2:
                    print(f"  > Intento {attempt + 1} fallido incluso sin verificación SSL: {e2}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
            except Exception as e:
                print(f"  > Intento {attempt + 1} fallido: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)

        if not response or not response.ok:
            continue

        ext = ".jpg"
        content_type = response.headers.get('content-type', '')
        if "image" in content_type:
            ext_part = content_type.split("/")[-1].split(";")[0].strip()
            ext = "." + ext_part if ext_part else ext

        file_name = f"{url_hash}{ext}"
        file_path = os.path.join(CACHE_DIR, file_name)

        try:
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(8192):
                    f.write(chunk)

            description = get_image_description(file_path, client)
            media_descriptions[src] = description
            cache[img_url] = {"local_path": file_path, "description": description}
            save_cache(cache)
            print("  > Imagen procesada y cacheada.")
        except Exception as e:
            print(f"  > Error procesando imagen {img_url}: {e}")

    return media_descriptions

