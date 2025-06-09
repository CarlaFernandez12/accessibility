import os

AXE_SCRIPT_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.8.4/axe.min.js"

IMAGE_DOMAIN_BLACKLIST = [
    "openstreetmap.org",
]

NODE_CHUNK_SIZE = 5

BASE_RESULTS_DIR = "results"
CACHE_DIR = "media_cache"
CACHE_FILE = os.path.join(CACHE_DIR, "cache.json")