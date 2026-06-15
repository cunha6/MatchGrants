import os
import re
import urllib.parse
import requests
from datetime import datetime
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from .docling_ocr import _clean_ocr

_DATETIME_RE = re.compile(r"^\d{8}_\d{6}_")

_pdf_opts = PdfPipelineOptions()
_pdf_opts.do_ocr = False
_pdf_opts.generate_page_images = False
_pdf_opts.generate_picture_images = False


_docling = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_options=_pdf_opts,
        )
    }
)


def _extract_filename(url: str) -> str:
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "filename" in params:
        return params["filename"][0]
    return url.split("/")[-1].split("?")[0] or "unknown.pdf"


def find_existing_document(url: str | None, download_dir: str) -> str | None:
    """Devolve o caminho do PDF já descarregado, ou None se ainda não existir."""
    if not url or not os.path.isdir(download_dir):
        return None

    original_name = _extract_filename(url)
    return next(
        (os.path.join(download_dir, f) for f in os.listdir(download_dir)
         if os.path.isfile(os.path.join(download_dir, f))
         and _DATETIME_RE.sub("", f) == original_name),
        None,
    )


def download_document(url: str | None, download_dir: str, url_pagina: str = "") -> str | None:
    if not url:
        return None

    original_name = _extract_filename(url)
    os.makedirs(download_dir, exist_ok=True)

    existing = find_existing_document(url, download_dir)
    if existing:
        print(f"Já existe: {original_name} — download ignorado.")
        return existing

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    extended_name = f"{timestamp}_{original_name}"
    path = os.path.join(download_dir, extended_name)
    try:
        print(f"A descarregar: {extended_name}...")
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        with open(path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        with open(path, "rb") as f:
            header = f.read(5)
        if not header.startswith(b"%PDF"):
            print(f"Ficheiro inválido (não é PDF): {extended_name} — removido.")
            os.remove(path)
            return None

        convert(path, download_dir, url_origem=url_pagina or url)
        return path
    except Exception as e:
        print(f"Erro: {e}")
        return None


def convert(path: str | None, download_dir: str = "", url_origem: str = "") -> str | None:
    subfolder = download_dir.split("/")[-1] if download_dir else ""
    md_dir = os.path.join("output", "markdown", subfolder) if subfolder else os.path.join("output", "markdown")
    os.makedirs(md_dir, exist_ok=True)

    source_name = os.path.splitext(os.path.basename(path))[0]
    md_path = os.path.join(md_dir, source_name + ".md")

    with open(path, "rb") as f:
        if not f.read(5).startswith(b"%PDF"):
            print(f"Ficheiro inválido (não é PDF): {os.path.basename(path)} — ignorado.")
            return None

    try:
        result = _docling.convert(path)
        markdown = result.document.export_to_markdown()
        markdown = _clean_ocr(markdown)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown)
    except Exception as e:
        print(f"Erro docling (conversão): {e}")
        return None

    try:
        from ..IA.pipeline import run_pipeline
        ai_data = run_pipeline(result.document, source_name)
        if ai_data:
            from ..db_service import save_ai_grant
            save_ai_grant(ai_data, scraping_url=url_origem, pdf_path=path, markdown_path=md_path)
    except Exception as e:
        print(f"Error saving to database: {e}")

    return md_path
