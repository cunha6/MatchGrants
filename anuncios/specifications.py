"""
Tender specifications ("Caderno de Encargos") downloader.

Given a notice's procedure-documents URL, finds and downloads the tender specifications
PDF into pdf_Anuncios/. Handles a direct PDF/ZIP and HTML pages — including JS SPAs
(e.g. Vortal) rendered with a headless browser. Kept separate to keep services.py lean.
"""

import io
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings

# Fonte única de normalização — reexportada porque services.py importa `normalize` daqui.
from common.text import normalize  # noqa: F401

# Selenium is optional: without it, JS-portal (Vortal) downloads are skipped.
try:
    from selenium.webdriver import Chrome
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_OK = True
except ImportError:
    SELENIUM_OK = False

logger = logging.getLogger(__name__)

TIMEOUT = 30  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Destination folder (relative to project root) and accepted document extensions.
SPECS_DIR = "pdf_Anuncios"
# Subpastas (dentro de SPECS_DIR) onde ficam, separados, o caderno de encargos e o programa
# de concurso.
SPECS_SUBDIR = "caderno_encargos"
PROGRAM_SUBDIR = "programa_concurso"
DOC_EXTENSIONS = (".pdf", ".docx")

# Specifications-name match (over the normalized name: lowercase, no accents,
# separators -> space):
#   STRONG -> says "caderno"/"encargo" or the "cad enc" abbreviation (e.g. anogov CAD_ENC).
#   WEAK   -> "ce" as an isolated token (also appears in annexes, e.g. "Anexo V do CE").
_SPECS_STRONG_RE = re.compile(r"caderno|encargo|cad[ _.\-]enc")
_SPECS_WEAK_RE = re.compile(r"\bce\b")

# Competition-program-name match ("Programa de Concurso" / "Programa do Procedimento"):
#   STRONG -> "programa (de) concurso|procedimento".
#   WEAK   -> the "PC"/"PP" abbreviation as an isolated token.
_PROGRAM_STRONG_RE = re.compile(r"programa\s+(de\s+|do\s+)?(concurso|procedimento)")
_PROGRAM_WEAK_RE = re.compile(r"\b(pc|pp)\b")

# Document download links inside a rendered SPA (e.g. Vortal).
_DOWNLOAD_LINK_XPATH = (
    "//a[contains(@href, 'token=') or contains(@href, 'PublicDownload') "
    "or contains(@class, 'buttonDownload')]"
)


# --- Text normalization / name matching ----------------------------------

def _specs_normalize(name: str) -> str:
    """Lowercase, no accents, separators (_-./) -> space."""
    return re.sub(r"[^a-z0-9]+", " ", normalize(name)).strip()


def is_specifications_strong(name: str) -> bool:
    """STRONG match: name contains 'caderno'/'encargo' (or the 'cad enc' abbreviation)."""
    return bool(name) and bool(_SPECS_STRONG_RE.search(_specs_normalize(name)))


def is_specifications(name: str) -> bool:
    """True if the name is a tender specifications doc (strong OR isolated 'CE')."""
    if not name:
        return False
    n = _specs_normalize(name)
    return bool(_SPECS_STRONG_RE.search(n) or _SPECS_WEAK_RE.search(n))


def is_program_strong(name: str) -> bool:
    """STRONG match: name says 'programa (de) concurso/procedimento'."""
    return bool(name) and bool(_PROGRAM_STRONG_RE.search(_specs_normalize(name)))


def is_program(name: str) -> bool:
    """True if the name is a competition-program doc (strong OR isolated 'PC'/'PP')."""
    if not name:
        return False
    n = _specs_normalize(name)
    return bool(_PROGRAM_STRONG_RE.search(n) or _PROGRAM_WEAK_RE.search(n))


# --- File helpers ---------------------------------------------------------

def _is_docx_internally(zip_bytes: bytes) -> bool:
    """A DOCX is a ZIP internally; check for its root XML."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            return "word/document.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _specs_dir(subdir: str = ""):
    d = settings.BASE_DIR / SPECS_DIR / subdir if subdir else settings.BASE_DIR / SPECS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url_filename(url: str) -> str:
    """File name from the URL (?filename= or last path segment)."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "filename" in params:
        return params["filename"][0]
    return unquote(parsed.path.split("/")[-1]) or "Caderno de Encargos.pdf"


def _save_bytes(name: str, content: bytes, subdir: str = "") -> str:
    """Save to pdf_Anuncios/[subdir/] with the original name prefixed by a timestamp.

    Format: '{YYYYMMDD_HHMMSS}_{original name}'. Returns the path relative to the root.
    `subdir` separa o caderno de encargos (SPECS_SUBDIR) do programa de concurso (PROGRAM_SUBDIR).
    """
    original = os.path.basename(name) or "documento.pdf"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{timestamp}_{original}"
    (_specs_dir(subdir) / fname).write_bytes(content)
    return f"{SPECS_DIR}/{subdir}/{fname}" if subdir else f"{SPECS_DIR}/{fname}"


# --- HTTP ------------------------------------------------------------------

def _get(url: str, cookies: dict | None = None, referer: str | None = None):
    """Tolerant GET (follows redirects); returns the Response or None on failure."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
        "Accept-Language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=headers, allow_redirects=True, cookies=cookies)
        r.raise_for_status()
        return r
    except requests.RequestException:
        return None


def _looks_pdf(resp) -> bool:
    """True if the response is a PDF (magic bytes or Content-Type)."""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    return resp.content[:4] == b"%PDF" or "application/pdf" in ctype


def _response_filename(resp) -> str:
    """File name: Content-Disposition if present, else derived from the final URL."""
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip())
    url_fn = _url_filename(resp.url)
    # If the URL-derived name is just "download" or a token, force a clean name.
    if "download" in url_fn.lower() or "token" in url_fn.lower():
        return "Caderno_de_Encargos.pdf"
    return url_fn


# --- Finders (ZIP / HTML) -------------------------------------------------

def _find_in_zip(zip_bytes: bytes, matcher=is_specifications, subdir: str = "") -> str:
    """Read the ZIP and extract the PDF matching `matcher` (or the first PDF as fallback)."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            pdfs = [i for i in zf.infolist()
                    if not i.is_dir() and i.filename.lower().endswith(DOC_EXTENSIONS)]
            if not pdfs:
                return ""
            for info in pdfs:  # prefer a file whose name matches
                if matcher(info.filename):
                    return _save_bytes(info.filename, zf.read(info), subdir)
            info = pdfs[0]  # fallback: first document, keep its name
            return _save_bytes(info.filename, zf.read(info), subdir)
    except zipfile.BadZipFile:
        return ""


def _pair_documents_in_zip(zip_bytes: bytes) -> dict:
    """De um ZIP de peças do procedimento, extrai AMBOS: o caderno de encargos e o programa de
    concurso. Quando o programa não está identificado pelo nome, é tomado como OUTRO PDF junto
    do caderno de encargos ("o pdf que está junto do CE"). Devolve {"specifications","program"}
    com os caminhos guardados (ou "" cada). Nunca aponta os dois para o mesmo ficheiro."""
    empty = {"specifications": "", "program": ""}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            pdfs = [i for i in zf.infolist()
                    if not i.is_dir() and i.filename.lower().endswith(DOC_EXTENSIONS)]
            if not pdfs:
                return empty
            program = next((i for i in pdfs if is_program(i.filename)), None)
            specs = next((i for i in pdfs if is_specifications(i.filename)), None)
            # Fallback do CE: 1.º PDF que não seja o programa (comportamento histórico: apanha
            # o 1.º PDF quando nenhum tem nome de caderno de encargos).
            if specs is None:
                specs = next((i for i in pdfs if i is not program), None)
            # Fallback do programa: outro PDF junto do caderno de encargos.
            if program is None and specs is not None:
                program = next((i for i in pdfs if i is not specs), None)
            return {
                "specifications": (_save_bytes(specs.filename, zf.read(specs), SPECS_SUBDIR)
                                   if specs else ""),
                "program": (_save_bytes(program.filename, zf.read(program), PROGRAM_SUBDIR)
                            if program and program is not specs else ""),
            }
    except zipfile.BadZipFile:
        return empty


def _find_in_html(html, base_url: str, cookies: dict | None = None,
                  matcher=is_specifications, subdir: str = "") -> str:
    """Row by row: if a table row mentions the wanted document (`matcher`), follow its link(s)."""
    soup = BeautifulSoup(html, "lxml")
    candidates = []
    for tr in soup.find_all("tr"):
        tr_text = tr.get_text(separator=" ", strip=True)
        if matcher(tr_text):
            for a in tr.find_all("a", href=True):
                href = a["href"].strip()
                if href.lower().startswith(("javascript:", "mailto:", "tel:", "#")):
                    continue
                candidates.append(urljoin(base_url, href))

    for full in list(dict.fromkeys(candidates))[:5]:
        resp = _get(full, cookies=cookies)
        if not resp:
            continue
        content = resp.content
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if _looks_pdf(resp):
            return _save_bytes(_response_filename(resp), content, subdir)
        if content[:2] == b"PK" and _is_docx_internally(content):
            return _save_bytes(_response_filename(resp), content, subdir)
        if content[:2] == b"PK" or "zip" in ctype:
            path = _find_in_zip(content, matcher=matcher, subdir=subdir)
            if path:
                return path
    return ""


# --- Headless browser (JS SPAs, e.g. Vortal) ------------------------------

def build_chrome():
    """Create a headless Chrome, or None if Selenium is unavailable/fails.

    In Docker the container ships 'chromium' + 'chromium-driver' (see Dockerfile): they
    are found on PATH and used (binary_location). Falls back to ChromeDriverManager
    (download) only when there is no local chromium — like scrape_portugal._build_driver.
    """
    if not SELENIUM_OK:
        return None

    opts = Options()
    for arg in ("--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                "--window-size=1920,1080", f"user-agent={USER_AGENT}"):
        opts.add_argument(arg)

    chromium_bin = shutil.which("chromium") or shutil.which("chromium-browser")
    chromedriver_bin = shutil.which("chromedriver")
    try:
        if chromium_bin and chromedriver_bin:
            opts.binary_location = chromium_bin  # Docker: system chromium + chromedriver
            return Chrome(service=Service(chromedriver_bin), options=opts)
        return Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    except WebDriverException as exc:
        logger.warning(f"[Selenium] Failed to start Chrome (Vortal will not extract): {exc}")
        return None


def _render_with_driver(driver, url: str):
    """Render the SPA and return (html, cookies) — event-based waits (fast)."""
    try:
        driver.get(url)
        # 1. Wait for the client-side navigation (token -> tender view).
        try:
            WebDriverWait(driver, 15).until(lambda d: d.current_url != url)
        except TimeoutException:
            pass
        # 2. Scroll to trigger lazy-loading of the documents section.
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        # 3. Smart wait: return as soon as a download link appears (up to 20s).
        try:
            WebDriverWait(driver, 20).until(
                lambda d: d.find_elements(By.XPATH, _DOWNLOAD_LINK_XPATH)
            )
        except TimeoutException:
            pass  # no documents rendered
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        return driver.page_source, cookies
    except WebDriverException:
        return None, None


def _render_html(url: str, driver=None):
    """Render a page with a headless browser (JS SPAs).

    If `driver` is given, reuse it (do NOT close it) — used by the import to share one
    Chrome across all notices. Otherwise create a Chrome just for this call and close it.
    Returns (html, cookies) or (None, None).
    """
    if driver is not None:
        return _render_with_driver(driver, url)
    own = build_chrome()
    if own is None:
        return None, None
    try:
        return _render_with_driver(own, url)
    finally:
        own.quit()


# --- Public entry point ---------------------------------------------------

def fetch_documents(url: str, driver_factory=None) -> dict:
    """Encontra e descarrega os DOIS documentos base do procedimento — caderno de encargos e
    programa de concurso — a partir do URL das peças, numa ÚNICA passagem (a página/SPA é
    obtida/renderizada uma só vez). Devolve {"specifications": path|"", "program": path|""}.

    Trata: ZIP direto (empareilha ambos), PDF direto (classifica pelo nome), HTML estático e,
    em fallback, página renderizada por browser (SPA, ex: Vortal). `driver_factory`: callable
    opcional que devolve um Chrome partilhado (reutilizado por todo o import).
    """
    empty = {"specifications": "", "program": ""}
    if not url:
        return empty
    resp = _get(url)
    if not resp:
        return empty

    content = resp.content
    ctype = (resp.headers.get("Content-Type") or "").lower()
    final_url = resp.url.lower().split("?")[0]

    # ZIP direto: empareilha CE + programa a partir dos PDFs lá dentro.
    if content[:2] == b"PK" or "zip" in ctype or final_url.endswith(".zip"):
        return _pair_documents_in_zip(content)

    # PDF direto: um único ficheiro — classifica-o como CE ou programa pelo nome.
    if _looks_pdf(resp):
        name = _response_filename(resp)
        if is_specifications(name):
            return {"specifications": _save_bytes(name, content, SPECS_SUBDIR), "program": ""}
        if is_program(name):
            return {"specifications": "", "program": _save_bytes(name, content, PROGRAM_SUBDIR)}
        return empty

    # HTML estático: procura os dois na MESMA página (sem re-obter). O caderno de encargos vai
    # para a subpasta SPECS_SUBDIR; o programa de concurso vai para a subpasta PROGRAM_SUBDIR.
    docs = {
        "specifications": _find_in_html(content, resp.url, matcher=is_specifications, subdir=SPECS_SUBDIR),
        "program": _find_in_html(content, resp.url, matcher=is_program, subdir=PROGRAM_SUBDIR),
    }
    if docs["specifications"] or docs["program"]:
        return docs

    # Fallback SPA: renderiza UMA vez e procura os dois no DOM final (com os cookies da sessão).
    driver = driver_factory() if driver_factory else None
    rendered, cookies = _render_html(resp.url, driver=driver)
    if rendered:
        return {
            "specifications": _find_in_html(rendered, resp.url, cookies=cookies,
                                            matcher=is_specifications, subdir=SPECS_SUBDIR),
            "program": _find_in_html(rendered, resp.url, cookies=cookies,
                                     matcher=is_program, subdir=PROGRAM_SUBDIR),
        }
    return empty


def fetch_specifications(url: str, driver_factory=None) -> str:
    """Retrocompatível: só o caderno de encargos. Ver `fetch_documents` (que obtém ambos)."""
    return fetch_documents(url, driver_factory=driver_factory)["specifications"]
