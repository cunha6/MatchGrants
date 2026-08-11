import io
import logging
import os
import re
import unicodedata
import urllib.parse
import requests
from datetime import datetime
from pypdf import PdfReader
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from .docling_ocr import _clean_ocr

logger = logging.getLogger(__name__)

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
        (os.path.join(download_dir, filename) for filename in os.listdir(download_dir)
         if os.path.isfile(os.path.join(download_dir, filename))
         and _DATETIME_RE.sub("", filename) == original_name),
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
    match = re.search(r"natureza\s+do\s+aviso\s*[:\-]?\s*([^\n]*(?:\n\s*[^\n]+){0,2})", low)
    if match:
        seg = match.group(1)
        i_conv, i_conc = seg.find("convite"), seg.find("concurso")
        if i_conv != -1 and (i_conc == -1 or i_conv < i_conc):
            return True
        if i_conc != -1:
            return False
    return bool(re.search(r"convite\s+(?:à|a)\s+apresenta", low))


def _bytes_is_invitation(pdf_bytes: bytes, max_pages: int = 5) -> bool:
    """True se a Natureza do aviso nas primeiras `max_pages` páginas indicar convite.

    Verificação BARATA (texto do pypdf), feita no download ANTES da conversão Docling —
    convites detetáveis aqui nem chegam a ser convertidos nem gravados em disco.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join((pdf_page.extract_text() or "") for pdf_page in reader.pages[:max_pages])
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
        logger.info("Já existe: %s — download ignorado.", original_name)
        return existing

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        pdf_bytes = response.content

        # Não é PDF (ex: link para anexo .docx/.zip) → passa à frente em silêncio
        if not pdf_bytes.startswith(b"%PDF"):
            return None

        # Convite → passa à frente em silêncio (nem aparece na consola)
        if reject_invitations and _bytes_is_invitation(pdf_bytes):
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(download_dir, f"{timestamp}_{original_name}")
        with open(path, "wb") as filename:
            filename.write(pdf_bytes)
        logger.info("Descarregado: %s", os.path.basename(path))
        return path
    except Exception as e:
        logger.error("Erro no download do PDF (%s): %s", url, e)
        return None


# Fórmulas de mérito (ex: "MP = 0,2 A + 0,3 B + 0,1 C + 0,4 D", "A = 0,6 A1 + 0,4 A2"):
# LHS = 'MP' ou um código de critério (letra + dígitos/pontos); RHS começa por um coeficiente
# numérico seguido de variável. O guard `=\s*\d` exclui a fórmula do GC (ex: "GC = (valor…)").
_MERIT_FORMULA_RE = re.compile(r"^(MP|[A-Za-z][A-Za-z0-9.]*)\s*=\s*\d[\d.,]*\s*[A-Za-z]")


def _acc(text: str) -> str:
    """minúsculas sem acentos (para casar âncoras com/sem acento)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(character for character in decomposed if not unicodedata.combining(character)).lower()


def _nows(text: str) -> str:
    """remove todo o espaço branco (para comparar fórmulas independente de espaçamento)."""
    return re.sub(r"\s+", "", text or "").lower()


def _raw_pdf_text(path: str) -> str:
    try:
        reader = PdfReader(path)
        return "\n".join((pdf_page.extract_text() or "") for pdf_page in reader.pages)
    except Exception:
        return ""


def rescue_merit_formulas(markdown: str, raw_text: str) -> str:
    """Reinjeta no markdown as fórmulas de mérito que o Docling deixou cair.

    O Docling perde fórmulas centradas/negrito (ex: "MP = 0,2 A + 0,3 B + …"), mas elas são
    TEXTO real no PDF — o pypdf lê-as. Aqui procuram-se essas fórmulas no texto cru e, as que
    faltam no markdown, são inseridas junto ao Referencial de Mérito (ou à frase que as
    introduz), para que o passo de avaliação (P4) as encontre com os pesos certos.
    """
    if not raw_text:
        return markdown

    formulas, seen = [], set()
    for line in raw_text.splitlines():
        text = line.strip()
        if _MERIT_FORMULA_RE.match(text):
            k = _nows(text)
            if k not in seen:
                seen.add(k)
                formulas.append(text)

    md_norm = _nows(markdown)
    missing = [filename for filename in formulas if _nows(filename) not in md_norm]
    if not missing:
        return markdown

    block = "\n".join(f"- {filename}" for filename in missing)
    insert = f"\n**Fórmulas (recuperadas do texto do PDF):**\n\n{block}\n"

    lines = markdown.splitlines(keepends=True)
    acc = [_acc(line) for line in lines]

    def find(pred):
        return next((i for i, a in enumerate(acc) if pred(a)), None)

    # Preferência: heading do referencial de mérito → frase "seguinte fórmula" → linha "em que:".
    idx = find(lambda a: a.lstrip().startswith("#") and "referencial" in a and "merito" in a)
    if idx is None:
        idx = find(lambda a: "seguinte formula" in a)
    if idx is None:
        idx = find(lambda a: a.strip() in ("em que:", "em que"))
    if idx is not None:
        lines.insert(idx + 1, insert)
        return "".join(lines)

    # Sem âncora: acrescenta no fim, sob uma secção que o router encaminha para o P4.
    return markdown + "\n\n## Referencial de Mérito — Fórmulas (recuperadas do PDF)\n" + insert


def pdf_to_markdown(path: str, download_dir: str = "") -> tuple[str, str] | None:
    """Converte um PDF em markdown limpo e grava-o. Devolve (markdown, md_path) ou None."""
    subfolder = download_dir.split("/")[-1] if download_dir else ""
    md_dir = os.path.join("output", "markdown", subfolder) if subfolder else os.path.join("output", "markdown")
    os.makedirs(md_dir, exist_ok=True)

    source_name = os.path.splitext(os.path.basename(path))[0]
    md_path = os.path.join(md_dir, source_name + ".md")

    with open(path, "rb") as filename:
        if not filename.read(5).startswith(b"%PDF"):
            return None

    try:
        result = _docling.convert(path)
        markdown = _clean_ocr(result.document.export_to_markdown())
        # Recupera fórmulas de mérito que o Docling deixa cair (texto centrado/negrito).
        markdown = rescue_merit_formulas(markdown, _raw_pdf_text(path))
        with open(md_path, "w", encoding="utf-8") as filename:
            filename.write(markdown)
        return markdown, md_path
    except Exception as e:
        logger.error("Erro docling (conversão de %s): %s", path, e)
        return None
