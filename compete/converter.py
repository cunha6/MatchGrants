import os
import re
import requests
from datetime import datetime
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

_pdf_opts = PdfPipelineOptions()
_pdf_opts.do_ocr = False
_pdf_opts.generate_page_images = False
_pdf_opts.generate_picture_images = False

_converter = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=_pdf_opts, backend=PyPdfiumDocumentBackend)
    }
)

DOWNLOAD_DIR = "avisos/compete"

_DATETIME_RE = re.compile(r"^\d{8}_\d{6}_")


def download_and_convert(url: str | None) -> str | None:
    if not url:
        return None

    original_name = url.split("/")[-1].split("?")[0]
    print(f"Último aviso: {original_name} | URL: {url}")

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    existing = next(
        (f for f in os.listdir(DOWNLOAD_DIR)
         if os.path.isfile(os.path.join(DOWNLOAD_DIR, f))
         and _DATETIME_RE.sub("", f) == original_name),
        None,
    )

    print(f"Verificando existência de: {original_name} → {'Encontrado' if existing else 'Não encontrado'}")

    doc_path = None
    if existing:
        print(f"Já existe: {original_name} — download ignorado.")
        doc_path = os.path.join(DOWNLOAD_DIR, existing)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        extended_name = f"{timestamp}_{original_name}"
        doc_path = os.path.join(DOWNLOAD_DIR, extended_name)
        try:
            print(f"A descarregar: {extended_name}...")
            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()
            with open(doc_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
        except Exception as e:
            print(f"Erro download: {e}")
            doc_path = None

    if doc_path:
        try:
            result = _converter.convert(doc_path)
            return result.document.export_to_markdown()
        except Exception as e:
            print(f"Erro docling: {e}")

    return None
