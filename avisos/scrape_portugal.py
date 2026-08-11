import io
import logging
import os
import re
import sys
import shutil
import tempfile
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from pypdf import PdfReader
from selenium import webdriver
from selenium.webdriver import Chrome
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException,
    UnexpectedAlertPresentException,
)

from common.docling.converter import text_is_invitation

logger = logging.getLogger(__name__)

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
    "--headless",              
    "--no-sandbox",            
    "--disable-dev-shm-usage", 
    "--disable-gpu",           
    "--disable-extensions",
    "--window-size=1920,1080",
    f"user-agent={_USER_AGENT}",
]

_KEY_MAP = {
    "Data de Publicação": "publication_date",
    "Data de Início": "opening_date",
    "Data de Fim": "closing_date",
    "Código do Aviso": "grant_code",
    "Objetivo Específico": "objective",
    "Dotação": "total_allocation",
    "Submissão de Candidaturas": "application_submission",
}


def _build_driver() -> webdriver.Remote:
    chrome_options = Options()
    for chrome_argument in _CHROME_ARGS:
        chrome_options.add_argument(chrome_argument)

    # Em Docker, o Chrome corre num container dedicado (selenium/standalone-chrome).
    # O chromium do Debian (v150) rebenta com SIGTRAP em headless/WSL2 — o Chrome da
    # imagem selenium é testado para container e funciona. Ligamos por WebDriver remoto.
    remote_webdriver_url = os.environ.get("SELENIUM_REMOTE_URL")
    if remote_webdriver_url:
        return webdriver.Remote(command_executor=remote_webdriver_url, options=chrome_options)

    # Fallback local (dev fora do Docker): perfil temporário ÚNICO e escrevível.
    chrome_options.add_argument(f"--user-data-dir={tempfile.mkdtemp(prefix='chrome-')}")
    chrome_options.binary_location = (
        shutil.which("chromium") or shutil.which("chromium-browser") or "/usr/bin/chromium")
    chromedriver_path = shutil.which("chromedriver") or "/usr/bin/chromedriver"

    # log_output=sys.stdout atira os erros do Chrome/chromedriver direto para o log do Docker.
    chromedriver_service = Service(chromedriver_path, log_output=sys.stdout)
    return Chrome(service=chromedriver_service, options=chrome_options)


def _dismiss_alert(driver) -> None:
    """Descarta um alerta aberto pelo site.

    Enquanto um alerta estiver aberto, QUALQUER chamada seguinte ao driver — incluindo o
    `page_source` — volta a falhar com UnexpectedAlertPresentException. Descartá-lo é o que
    nos deixa ficar com o HTML já carregado.
    """
    try:
        driver.switch_to.alert.accept()
    except Exception:
        pass


def _load_all_pages(driver) -> int:
    """Clica em "Carregar mais" até esgotar a listagem. Devolve o nº de cliques feitos.

    Para em três situações, todas tratadas como fim NORMAL da listagem — o que já foi
    carregado é aproveitado, em vez de se perder o scrape inteiro:
      - o botão desaparece (chegámos ao fim) ou fica inacessível;
      - o PRÓPRIO SITE mostra um alerta de erro. O portugal2030.pt faz isso quando o backend
        dele falha (ex: "502: erro a ir buscar os avisos") — é uma falha do lado de lá, não
        nossa, e não deve deitar abaixo um scrape que já tem avisos.
    """
    clicks = 0
    while True:
        # Duas tentativas: uma rápida e, se falhar, uma mais paciente antes de desistir.
        for timeout in (2, 7):
            try:
                clicks = _wait_click(driver, clicks, timeout)
                break
            except TimeoutException:
                continue
            except (NoSuchElementException, StaleElementReferenceException):
                logger.info("[Portugal] botão indisponível após %d cliques.", clicks)
                return clicks
            except UnexpectedAlertPresentException as exc:
                logger.warning(
                    "[Portugal] o site interrompeu o carregamento (%s) — sigo com o que já "
                    "foi carregado em %d cliques.", getattr(exc, "alert_text", exc), clicks)
                _dismiss_alert(driver)
                return clicks
        else:
            logger.info("[Portugal] botão desapareceu após %d cliques.", clicks)
            return clicks


def scrape_portugal2030_web() -> list[dict]:
    listing_html = _fetch(LIST_URL)
    if not listing_html:
        return []

    driver = _build_driver()
    try:
        driver.get(LIST_URL)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, LOAD_MORE_XPATH))
        )
        _load_all_pages(driver)
        fully_loaded_html = driver.page_source
    finally:
        driver.quit()

    return _parse_main(fully_loaded_html)


# Nomes de documento que NUNCA são o aviso em si (são acessórios), e os que sugerem que o
# são. Usados na escolha do candidato a aviso canónico (ver _pick_candidates).
_WORDS_REJECT = {"prorrogação", "prorrogacao", "guia", "anexo", "orientação", "orientacao",
                 "verificação", "verificacao"}
_WORDS_SEARCH = {"aviso", "aac", "rep", "republica", "alteracao"}


def _extract_fields(grant_item, grant_record: dict) -> None:
    """Preenche `grant_record` com os pares <dt>/<dd> da ficha do aviso, traduzindo as
    etiquetas do site pelos nomes internos (_KEY_MAP). A secção "documentos" é tratada à parte."""
    for container in grant_item.find_all("div"):
        for definition_list in container.find_all("dl"):
            if not hasattr(definition_list, "find"):
                continue
            label_element = definition_list.find("dt")
            if not label_element:
                continue
            label_text = label_element.get_text(strip=True)
            if not label_text or "documentos" in label_text.lower():
                continue
            value_element = label_element.find_next_sibling("dd")
            grant_record[_KEY_MAP.get(label_text, label_text)] = (
                value_element.get_text(strip=True) if value_element else "")


def _extract_documents(grant_item) -> list[dict]:
    """Documentos do aviso, do mais recente para o mais antigo (o site lista-os ao contrário)."""
    documents_container = grant_item.find("div", class_="avisos-docs")
    if not documents_container:
        return []
    # href=True evita KeyError em <a> sem href; URLs já absolutos não levam prefixo.
    return [
        {"nome": document_link.get_text(strip=True),
         "url": (document_link["href"] if document_link["href"].startswith("http")
                 else BASE_URL + document_link["href"])}
        for document_link in reversed(documents_container.find_all("a", href=True))
    ]


def _pick_candidates(grant_code: str, documents: list[dict]) -> list[dict]:
    """Documentos que podem ser o aviso canónico.

    Havendo um único PDF, é esse. Senão, aceita os que tragam o número do código do aviso no
    nome OU um termo típico de aviso, desde que não sejam acessórios (guia, anexo,
    prorrogação...). A verificação cara — abrir o PDF — fica para depois, só sobre estes.
    """
    pdf_documents = [doc for doc in documents if doc["nome"].lower().endswith(".pdf")]
    if len(pdf_documents) == 1:
        return pdf_documents

    grant_code_digits = re.sub(r'^[a-z]+', '', normalize(grant_code or ""))
    candidate_documents = []
    for document in documents:
        document_name = document["nome"].lower()
        has_grant_code = (grant_code_digits != ""
                          and grant_code_digits in normalize(document["nome"]))
        has_notice_term = any(term in document_name for term in _WORDS_SEARCH)
        is_not_accessory = not any(term in document_name for term in _WORDS_REJECT)
        if (has_grant_code or has_notice_term) and is_not_accessory:
            candidate_documents.append(document)
    return candidate_documents


def _parse_grant_item(grant_item) -> dict | None:
    """Um <li> da listagem → registo do aviso (com os candidatos ainda por verificar).
    None quando o <li> não tem título (não é um aviso)."""
    title_element = grant_item.find("strong")
    if not title_element:
        return None

    grant_record = {"source": "Portugal2030", "title": title_element.get_text(strip=True)}
    _extract_fields(grant_item, grant_record)
    documents = _extract_documents(grant_item)
    grant_record["documentos"] = documents
    grant_record["_candidatos"] = _pick_candidates(grant_record.get("grant_code", ""), documents)
    return grant_record


def _verify_pdfs(candidate_urls: set[str]) -> dict[str, dict]:
    """Abre em PARALELO todos os PDFs candidatos e devolve {url: info}.

    É a parte cara do scrape (uma descarga por PDF), daí correr em paralelo e uma só vez por
    URL — o mesmo documento pode ser candidato em vários avisos.
    """
    logger.info("[Portugal] A verificar %d PDFs em paralelo...", len(candidate_urls))
    pdf_info_by_url: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        url_by_download = {executor.submit(_pdf_info, url): url for url in candidate_urls}
        for finished_download in as_completed(url_by_download):
            pdf_info_by_url[url_by_download[finished_download]] = finished_download.result()
    return pdf_info_by_url


def _assign_latest_notice(grant_records: list[dict], pdf_info_by_url: dict[str, dict]) -> None:
    """Escolhe o aviso canónico de cada registo: o 1º candidato que seja um PDF com corpo
    (>4 páginas) e não seja um convite. Sem nenhum a cumprir, fica None."""
    for grant_record in grant_records:
        grant_record["latest_notice"] = None
        for candidate_document in grant_record.pop("_candidatos", []):
            pdf_info = pdf_info_by_url.get(
                candidate_document["url"], {"paginas": 0, "natureza": None})
            if pdf_info["paginas"] > 4 and pdf_info["natureza"] != "convite":
                grant_record["latest_notice"] = candidate_document
                break


def _parse_main(html: str) -> list[dict]:
    """Avisos da listagem do Portugal2030, em três passagens: ler o HTML (sem rede), abrir os
    PDFs candidatos em paralelo, e só então escolher o aviso canónico de cada um."""
    parsed_page = BeautifulSoup(html, "lxml")
    listing_container = parsed_page.find("div", class_="et_pb_column_2_tb_body")
    if listing_container is None:
        # Estrutura da página não encontrada (mudou, ou não renderizou). Não rebenta o scrape.
        logger.warning("[Portugal] container 'et_pb_column_2_tb_body' não encontrado — 0 avisos.")
        return []

    grant_records = [
        grant_record
        for grant_record in (_parse_grant_item(item)
                             for item in listing_container.find_all("li"))
        if grant_record is not None
    ]
    candidate_urls = {document["url"]
                      for grant_record in grant_records
                      for document in grant_record["_candidatos"]}

    _assign_latest_notice(grant_records, _verify_pdfs(candidate_urls))
    return grant_records


def _fetch(url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            http_response = requests.get(url, headers=_HEADERS, timeout=30)
            http_response.raise_for_status()
            http_response.encoding = "utf-8"
            if not http_response.text:
                raise ValueError("Empty response")
            return http_response.text
        except (requests.RequestException, ValueError):
            if attempt < retries:
                time.sleep(3 * attempt)
    return None


def _wait_click(driver: Chrome, clicks_so_far: int, timeout: int) -> int:
    """Clica no "Carregar mais" e devolve o total de cliques. O clique e feito por JavaScript
    porque o botao pode estar coberto por elementos fixos da pagina."""
    load_more_button = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, LOAD_MORE_XPATH))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", load_more_button)
    driver.execute_script("arguments[0].click();", load_more_button)
    clicks_so_far += 1
    logger.info("Carregar mais clicado (%dx)", clicks_so_far)
    time.sleep(timeout)
    return clicks_so_far


def _pdf_info(url: str) -> dict:
    try:
        http_response = requests.get(url, headers=_HEADERS, timeout=30)
        http_response.raise_for_status()
        if not http_response.content.startswith(b"%PDF"):
            return {"paginas": 0, "natureza": None}
        pdf_reader = PdfReader(io.BytesIO(http_response.content))  # leitura em memoria
        first_pages_text = " ".join(
            page.extract_text() or "" for page in pdf_reader.pages[:5])
        # Deteção PRECISA (lê o campo "Natureza do aviso") — não exclui concursos que apenas
        # mencionem a palavra "convite". Mesma função usada no download/service.
        nature = "convite" if text_is_invitation(first_pages_text) else None
        return {"paginas": len(pdf_reader.pages), "natureza": nature}
    except Exception:
        return {"paginas": 0, "natureza": None}


def normalize(text: str) -> str:
    """Reduz um texto as suas letras e numeros, para comparar nomes de documento com codigos
    de aviso: minusculas, sem pontuacao, e com os numeros sem zeros a esquerda."""
    if not text:
        return ""
    alphanumeric_parts = re.findall(r'[a-zA-Z]+|\d+', text)
    return "".join(str(int(part)) if part.isdigit() else part.lower()
                   for part in alphanumeric_parts)
