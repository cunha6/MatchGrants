import os
import re
import urllib.parse
import requests
from datetime import datetime

DOWNLOAD_DIR = "avisos/portugal"

_DATETIME_RE = re.compile(r"^\d{8}_\d{6}_")


def download_document(url: str | None) -> str | None:
    if not url:
        return None

    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    original_name = params.get("filename", ["unknown.pdf"])[0]

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    already_exists = any(
        _DATETIME_RE.sub("", f) == original_name
        for f in os.listdir(DOWNLOAD_DIR)
        if os.path.isfile(os.path.join(DOWNLOAD_DIR, f))
    )

    if already_exists:
        print(f"Já existe: {original_name} — download ignorado.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    extended_name = f"{timestamp}_{original_name}"
    path = os.path.join(DOWNLOAD_DIR, extended_name)
    try:
        print(f"A descarregar: {extended_name}...")
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        with open(path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return path
    except Exception as e:
        print(f"Erro: {e}")
        return None
