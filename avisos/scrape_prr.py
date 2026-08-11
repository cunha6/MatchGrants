import logging
import os
import re
import shutil
import time
import urllib.parse
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)

BASE_URL = "https://recuperarportugal.gov.pt"
LIST_URL = f"{BASE_URL}/candidaturas-prr/#1624904725473-df8f5226-5d16/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9",
}

DATE_REGEX = re.compile(r"\d{2}/\d{2}/\d{4}")
REPUB_REGEX = re.compile(r"Republicação", re.IGNORECASE)
NUMBER_REPUB_REGEX = re.compile(r"(?:\d+[ªº.]*\s*)?Republicação", re.IGNORECASE)
GRANT_CODE_REGEX = re.compile(r"\b(\d+/[\w-]+/\d{4})\b")
CLOSING_KEYWORDS = ("encerramento", "fim de", "submissão", "até", "prazo")
DOCS_STOP_REGEX = re.compile(r"data de ", re.IGNORECASE)

_MONTH_NUMBER_BY_NAME = {
    "janeiro": "01", "fevereiro": "02", "março": "03", "abril": "04",
    "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
    "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
}


def _normalize_date(paragraph_text: str) -> str:
    hour_suffix = ""
    hour_match = re.search(r"(\d{1,2})[h:](\d{2})", paragraph_text)
    if hour_match:
        hour_suffix = f" {hour_match.group(1).zfill(2)}:{hour_match.group(2)}h"

    match = re.search(r"(\d{2}/\d{2}/\d{4})", paragraph_text)
    if match:
        return match.group(1) + hour_suffix

    match = re.search(r"(\d{1,2})\s+de\s+(\w+)(?:\s+de)?\s+(\d{4})", paragraph_text, re.IGNORECASE)
    if match:
        day, month_name, year = match.group(1), match.group(2).lower(), match.group(3)
        month_number = _MONTH_NUMBER_BY_NAME.get(month_name)
        if month_number:
            return f"{day.zfill(2)}/{month_number}/{year}" + hour_suffix

    return ""


def _get_driver() -> WebDriver:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    options.add_argument("--lang=pt-PT")
    options.add_experimental_option("prefs", {"intl.accept_languages": "pt-PT,pt"})

    # Em Docker: Chrome no container dedicado (selenium/standalone-chrome). O chromium
    # do Debian rebenta com SIGTRAP em headless/WSL2 — ligamos por WebDriver remoto.
    remote_url = os.environ.get("SELENIUM_REMOTE_URL")
    if remote_url:
        return webdriver.Remote(command_executor=remote_url, options=options)

    chromium_bin = shutil.which("chromium") or shutil.which("chromium-browser")
    chromedriver_bin = shutil.which("chromedriver")
    if chromium_bin and chromedriver_bin:
        options.binary_location = chromium_bin
        return WebDriver(service=Service(chromedriver_bin), options=options)

    return WebDriver(service=Service(ChromeDriverManager().install()), options=options)


def scrape_prr_web() -> list[dict]:
    scraped_grants: list[dict] = []
    logger.info("A iniciar scraping do PRR...")

    driver = _open_grants()
    try:
        _parse_grants(driver, scraped_grants)
    finally:
        driver.quit()

    return scraped_grants


def _open_grants() -> WebDriver:
    driver = _get_driver()
    driver.get(LIST_URL)
    wait = WebDriverWait(driver, 20)

    aberto_btn = wait.until(EC.element_to_be_clickable((By.ID, "aberto-btn")))
    aberto_btn.click()
    logger.info("Clicou em 'Abertos'")
    time.sleep(1)

    pesquisar_btn = wait.until(EC.element_to_be_clickable((By.ID, "pesquisar-btn")))
    pesquisar_btn.click()
    logger.info("Clicou em 'Pesquisar Avisos'")
    time.sleep(2)

    return driver


def _collect_documents(paragraph, seen_document_urls: set[str], documents: list) -> None:
    """Acrescenta os links de um parágrafo à lista de documentos, sem repetir.

    O nome sai do `?filename=` do URL; sem ele, do último segmento do caminho; e em último
    caso do texto da própria ligação. `mailto:` não é documento.
    """
    for document_link in paragraph.find_all("a", href=True):
        document_url = document_link["href"]
        if document_url in seen_document_urls or document_url.startswith("mailto:"):
            continue
        seen_document_urls.add(document_url)
        parsed_url = urllib.parse.urlparse(document_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        document_name = (query_params.get("filename", [None])[0] or parsed_url.path.split("/")[-1]
                or document_link.get_text(strip=True))
        documents.append({"nome": document_name, "url": document_url})


def _closing_date_from(paragraph_text: str) -> str | None:
    """Data-limite de um parágrafo que fala de fim de candidaturas.

    Havendo datas, vale a ÚLTIMA (o texto costuma referir primeiro a abertura), com a hora
    junta quando existe. Sem datas reconhecíveis, tenta normalizar o texto por extenso.
    """
    found_dates = DATE_REGEX.findall(paragraph_text)
    if not found_dates:
        return _normalize_date(paragraph_text) or None
    hour_suffix = ""
    hour_match = re.search(r"(\d{1,2})[h:](\d{2})", paragraph_text)
    if hour_match:
        hour_suffix = f" {hour_match.group(1).zfill(2)}:{hour_match.group(2)}h"
    return found_dates[-1] + hour_suffix


def _parse_card_paragraphs(card_paragraphs, title_href: str) -> dict:
    """Percorre os parágrafos do cartão, do fim para o início, recolhendo os detalhes.

    Ao contrário (`reversed`) porque o conteúdo mais RECENTE — a última republicação, o
    contacto e a documentação atuais — está no fim do cartão; o primeiro que casar ganha, e
    os `is None` abaixo garantem que um valor já encontrado não é substituído por um mais
    antigo. Os documentos deixam de ser recolhidos assim que se atinge o marcador de fim da
    secção (DOCS_STOP_REGEX).
    """
    found_details = {"last_republication": None, "last_republication_date": None,
             "latest_notice": None, "contact": None, "closing_date": None}
    seen_document_urls = {title_href}
    documents = []
    documents_section_ended = False

    for paragraph in reversed(card_paragraphs):
        paragraph_text = paragraph.get_text(strip=True)
        lowered_text = paragraph_text.lower()

        if not documents_section_ended:
            if DOCS_STOP_REGEX.search(paragraph_text):
                documents_section_ended = True
            else:
                _collect_documents(paragraph, seen_document_urls, documents)

        if found_details["last_republication_date"] is None and REPUB_REGEX.search(paragraph_text):
            republication_label_match = NUMBER_REPUB_REGEX.search(paragraph_text)
            republication_date_match = DATE_REGEX.search(paragraph_text)
            found_details["last_republication"] = republication_label_match.group(0) if republication_label_match else None
            found_details["last_republication_date"] = republication_date_match.group(0) if republication_date_match else None

        if found_details["latest_notice"] is None and "ver documentação" in lowered_text:
            anchor = paragraph.find("a")
            found_details["latest_notice"] = anchor.get("href") if anchor else None

        if found_details["contact"] is None and "contacto para informações" in lowered_text:
            anchor = paragraph.find("a")
            found_details["contact"] = anchor.get_text(strip=True) if anchor else None

        if found_details["closing_date"] is None and any(k in lowered_text for k in CLOSING_KEYWORDS):
            found_details["closing_date"] = _closing_date_from(paragraph_text)

    found_details["documentos"] = documents
    return found_details


def _parse_grant_card(grant_card, grant_type: str, grant_section: str) -> dict | None:
    """Um cartão de aviso → registo. None se o cartão não tiver título (nada a aproveitar)."""
    title_link = grant_card.find("a", class_="title-link")
    if not title_link:
        return None

    grant_title = title_link.get_text(strip=True)
    grant_code_match = GRANT_CODE_REGEX.search(grant_title)
    card_paragraphs = grant_card.find_all("p")

    # Abertura: a 1ª data que apareça no cartão (os parágrafos vêm por ordem cronológica).
    opening_date = next(
        (DATE_REGEX.search(p.get_text(strip=True)).group(0)
         for p in card_paragraphs
         if DATE_REGEX.search(p.get_text(strip=True))),
        None,
    )

    found_details = _parse_card_paragraphs(card_paragraphs, title_link.get("href", ""))
    return {
        "source": "PRR",
        "tipo": grant_type,
        "subtitulo": grant_section,
        "title": grant_title,
        "grant_code": grant_code_match.group(1) if grant_code_match else None,
        "opening_date": opening_date,
        "closing_date": found_details["closing_date"],
        "last_republication": found_details["last_republication"],
        "last_republication_date": found_details["last_republication_date"],
        "contact": found_details["contact"],
        "documentos": found_details["documentos"],
        "latest_notice": found_details["latest_notice"],
    }


def _parse_grants(driver: WebDriver, scraped_grants: list[dict]) -> None:
    """Percorre a página do PRR (painéis por tipo → secções → cartões) e acumula os avisos."""
    parsed_page = BeautifulSoup(driver.page_source, "lxml")
    type_panels = parsed_page.find_all("div", class_="vc_tta-panel", attrs={"data-vc-content": ".vc_tta-panel-body"})

    for type_panel in type_panels:
        # Guard: sem o span do título (HTML mudou), segue com tipo vazio em vez de rebentar.
        type_title_span = type_panel.find("span", class_="vc_tta-title-text")
        grant_type = type_title_span.get_text(strip=True) if type_title_span else ""

        for section_block in type_panel.find_all("div", class_=["entidadespublicas", "empresaspublicas", "ben", "accordion"]):
            section_title_element = section_block.find("div", id="prr-arrow-chev", class_="arrow-chev")
            grant_section = section_title_element.get_text(strip=True) if section_title_element else ""

            section_panel = section_block.find("div", class_="panel")
            if not section_panel:
                continue

            for grant_card in section_panel.find_all("div", class_=["search-card-top", "search-card"]):
                grant_record = _parse_grant_card(grant_card, grant_type, grant_section)
                if grant_record is not None:
                    scraped_grants.append(grant_record)
