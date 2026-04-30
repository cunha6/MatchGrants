import io
import re
import shutil
import time
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://portugal2030.pt"
LIST_URL = f"{BASE_URL}/avisos/"
LOAD_MORE_XPATH = "//a[normalize-space(text())='Carregar mais']"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HEADERS = {"User-Agent": _USER_AGENT}

_CHROME_ARGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--window-size=1920,1080",
    f"user-agent={_USER_AGENT}",
]

_KEY_MAP = {
    "Data de Publicação": "data_publicacao",
    "Data de Início": "data_inicio",
    "Data de Fim": "fim_candidatura",
    "Código do Aviso": "codigo_aviso",
    "Objetivo Específico": "objetivo",
    "Dotação": "dotacao",
    "Submissão de Candidaturas": "submissao_candidaturas",
}


def _build_driver() -> Chrome:
    opts = Options()
    for arg in _CHROME_ARGS:
        opts.add_argument(arg)

    chromium_bin = shutil.which("chromium") or shutil.which("chromium-browser")
    chromedriver_bin = shutil.which("chromedriver")
    if chromium_bin and chromedriver_bin:
        opts.binary_location = chromium_bin
        return Chrome(service=Service(chromedriver_bin), options=opts)

    return Chrome(service=Service(ChromeDriverManager().install()), options=opts)


def scrape_portugal2030_web() -> list[dict]:
    html = _fetch(LIST_URL)
    if not html:
        return []

    driver = _build_driver()
    try:
        driver.get(LIST_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, LOAD_MORE_XPATH))
        )
        clicks = 0
        while True:
            try:
                clicks = _wait_click(driver, clicks, 2)
            except TimeoutException:
                try:
                    clicks = _wait_click(driver, clicks, 7)
                except (TimeoutException, NoSuchElementException, StaleElementReferenceException):
                    print(f"Botão desapareceu após {clicks} cliques.")
                    break
        page_source = driver.page_source
    finally:
        driver.quit()

    return _parse_main(page_source)


def _parse_main(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    main_element = soup.find("div", class_="et_pb_column_2_tb_body")
    avisos = main_element.find_all("li")

    result = []
    for aviso in avisos:
        strong = aviso.find("strong")
        if not strong:
            continue

        data = {}
        data["origem"] = "Portugal2030"
        data["titulo"] = strong.get_text(strip=True)

        for line in aviso.find_all("div"):
            for label in line.find_all("dl"):
                if not hasattr(label, "find"):
                    continue
                dt = label.find("dt")
                if not dt:
                    continue
                dt_text = dt.get_text(strip=True)
                if not dt_text or "documentos" in dt_text.lower():
                    continue
                dd = dt.find_next_sibling("dd")
                data[_KEY_MAP.get(dt_text, dt_text)] = dd.get_text(strip=True) if dd else ""

        docs_div = aviso.find("div", class_="avisos-docs")
        documentos = [
            {"nome": a.get_text(strip=True), "url": BASE_URL + a["href"]}
            for a in reversed(docs_div.find_all("a"))
        ] if docs_div else []
        data["documentos"] = documentos

        words_reject = {"prorrogação", "prorrogacao", "guia", "anexo", "orientação", "orientacao", "verificação", "verificacao"}
        words_search = {"aviso", "aac", "rep", "republica", "alteracao"}
        normalize_code = normalize(data.get("codigo_aviso", ""))
        codigo_numeros = re.sub(r'^[a-z]+', '', normalize_code)

        data["ultimo_aviso"] = None

        pdfs = [doc for doc in documentos if doc["nome"].lower().endswith(".pdf")]
        if len(pdfs) == 1 and _pdf_pages(pdfs[0]["url"]) > 4:
            data["ultimo_aviso"] = pdfs[0]

        for doc in (documentos if data["ultimo_aviso"] is None else []):
            nome_doc = doc["nome"].lower()
            nome_doc_normalized = normalize(doc["nome"])
            has_codigo = codigo_numeros != "" and codigo_numeros in nome_doc_normalized
            has_search_term = any(termo in nome_doc for termo in words_search)
            has_no_reject_term = not any(rejeicao in nome_doc for rejeicao in words_reject)

            if (has_codigo or has_search_term) and has_no_reject_term:
                if _pdf_pages(doc["url"]) > 4:
                    data["ultimo_aviso"] = doc
                    break

        result.append(data)

    return result


def _fetch(url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            if not r.text:
                raise ValueError("Empty response")
            return r.text
        except (requests.RequestException, ValueError):
            if attempt < retries:
                time.sleep(3 * attempt)
    return None


def _wait_click(driver: Chrome, clicks: int, timeout: int) -> int:
    btn = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, LOAD_MORE_XPATH))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    driver.execute_script("arguments[0].click();", btn)
    clicks += 1
    print(f"Carregar mais clicado ({clicks}x)")
    time.sleep(timeout)
    return clicks


def _pdf_pages(url: str) -> int:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        return len(PdfReader(io.BytesIO(r.content)).pages)
    except Exception:
        return 0


def normalize(texto: str) -> str:
    if not texto:
        return ""
    parts = re.findall(r'[a-zA-Z]+|\d+', texto)
    return "".join(str(int(p)) if p.isdigit() else p.lower() for p in parts)
