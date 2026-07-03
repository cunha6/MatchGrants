import io
import os
import re
import urllib.parse
import requests
from datetime import datetime
from pypdf import PdfReader
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


def text_is_invitation(text: str) -> bool:
    """True se a Natureza/Modalidade do aviso for 'convite' (não queremos convites).

    Deteção PRECISA: lê o valor do campo "Natureza do aviso" (na mesma linha ou nas linhas
    logo a seguir ao rótulo) em vez de procurar a palavra 'convite' em qualquer sítio — assim
    não rejeita concursos que apenas a mencionem, e apanha convites cujo documento se chama
    "Aviso". Também aceita a frase direta "convite à apresentação de candidaturas".
    """
    if not text:
        return False
    low = text.lower()
    m = re.search(r"natureza\s+do\s+aviso\s*[:\-]?\s*([^\n]*(?:\n\s*[^\n]+){0,2})", low)
    if m:
        seg = m.group(1)
        i_conv, i_conc = seg.find("convite"), seg.find("concurso")
        if i_conv != -1 and (i_conc == -1 or i_conv < i_conc):
            return True
        if i_conc != -1:
            return False
    return bool(re.search(r"convite\s+(?:à|a)\s+apresenta", low))


def _bytes_is_invitation(data: bytes, max_pages: int = 5) -> bool:
    """True se a Natureza do aviso nas primeiras `max_pages` páginas indicar convite.

    Verificação BARATA (texto do pypdf), feita no download ANTES da conversão Docling —
    convites detetáveis aqui nem chegam a ser convertidos nem gravados em disco.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        text = " ".join((p.extract_text() or "") for p in reader.pages[:max_pages])
        return text_is_invitation(text)
    except Exception:
        return False


def download_pdf(url: str | None, download_dir: str, reject_invitations: bool = False) -> str | None:
    """
    Descarrega o PDF para memória (temporário), valida e — se `reject_invitations` —
    verifica nas primeiras 5 páginas se é um convite. Só grava no disco (download
    efetivo) se NÃO for convite. Devolve o caminho local ou None.
    """
    if not url:
        return None

    original_name = _extract_filename(url)
    os.makedirs(download_dir, exist_ok=True)

    existing = find_existing_document(url, download_dir)
    if existing:
        print(f"Já existe: {original_name} — download ignorado.")
        return existing

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.content

        # Não é PDF (ex: link para anexo .docx/.zip) → passa à frente em silêncio
        if not data.startswith(b"%PDF"):
            return None

        # Convite → passa à frente em silêncio (nem aparece na consola)
        if reject_invitations and _bytes_is_invitation(data):
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(download_dir, f"{timestamp}_{original_name}")
        with open(path, "wb") as f:
            f.write(data)
        print(f"Descarregado: {os.path.basename(path)}")
        return path
    except Exception as e:
        print(f"Erro: {e}")
        return None


def pdf_to_markdown(path: str, download_dir: str = "") -> tuple[str, str] | None:
    """Converte um PDF em markdown limpo e grava-o. Devolve (markdown, md_path) ou None."""
    subfolder = download_dir.split("/")[-1] if download_dir else ""
    md_dir = os.path.join("output", "markdown", subfolder) if subfolder else os.path.join("output", "markdown")
    os.makedirs(md_dir, exist_ok=True)

    source_name = os.path.splitext(os.path.basename(path))[0]
    md_path = os.path.join(md_dir, source_name + ".md")

    with open(path, "rb") as f:
        if not f.read(5).startswith(b"%PDF"):
            return None

    try:
        result = _docling.convert(path)
        markdown = _clean_ocr(result.document.export_to_markdown())
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown)
        return markdown, md_path
    except Exception as e:
        print(f"Erro docling (conversão): {e}")
        return None


def download_document(url: str | None, download_dir: str, url_pagina: str = "") -> str | None:
    """Fluxo single-doc (compatibilidade): download → markdown → pipeline → BD."""
    path = download_pdf(url, download_dir)
    if not path:
        return None

    converted = pdf_to_markdown(path, download_dir)
    if not converted:
        return path
    markdown, md_path = converted
    source_name = os.path.splitext(os.path.basename(path))[0]

    try:
        from ..IA.pipeline import run_pipeline
        ai_data = run_pipeline(markdown, source_name)
        if ai_data:
            from ..db_service import save_ai_grant
            save_ai_grant(ai_data, scraping_url=url_pagina or url, pdf_path=path, markdown_path=md_path)
    except Exception as e:
        print(f"Error saving to database: {e}")

    return path
