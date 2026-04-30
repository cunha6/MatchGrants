import re
import shutil
import time
from bs4 import BeautifulSoup
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

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
CODIGO_AVISO_REGEX = re.compile(r"\b(\d+/[\w-]+/\d{4})\b")


def _get_driver() -> WebDriver:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    options.add_argument("--lang=pt-PT")
    options.add_experimental_option("prefs", {"intl.accept_languages": "pt-PT,pt"})

    chromium_bin = shutil.which("chromium") or shutil.which("chromium-browser")
    chromedriver_bin = shutil.which("chromedriver")
    if chromium_bin and chromedriver_bin:
        options.binary_location = chromium_bin
        return WebDriver(service=Service(chromedriver_bin), options=options)

    return WebDriver(service=Service(ChromeDriverManager().install()), options=options)


def scrape_prr_web() -> list[dict]:
    all_data: list[dict] = []
    print("A iniciar scraping do PRR...")

    driver = _open_grants()
    try:
        _parse_grants(driver, all_data)
    finally:
        driver.quit()

    return all_data


def _open_grants() -> WebDriver:
    driver = _get_driver()
    driver.get(LIST_URL)
    wait = WebDriverWait(driver, 20)

    aberto_btn = wait.until(EC.element_to_be_clickable((By.ID, "aberto-btn")))
    aberto_btn.click()
    print("Clicou em 'Abertos'")
    time.sleep(1)

    pesquisar_btn = wait.until(EC.element_to_be_clickable((By.ID, "pesquisar-btn")))
    pesquisar_btn.click()
    print("Clicou em 'Pesquisar Avisos'")
    time.sleep(2)

    return driver


def _parse_grants(driver: WebDriver, all_data: list[dict]) -> None:
    soup = BeautifulSoup(driver.page_source, "lxml")
    panels = soup.find_all("div", class_="vc_tta-panel", attrs={"data-vc-content": ".vc_tta-panel-body"})

    for type_panel in panels:
        grants_type = type_panel.find("span", class_="vc_tta-title-text").get_text(strip=True)

        for grant_subdiv in type_panel.find_all("div", class_=["entidadespublicas", "empresaspublicas", "ben", "accordion"]):
            chev = grant_subdiv.find("div", id="prr-arrow-chev", class_="arrow-chev")
            grants_subtitle = chev.get_text(strip=True) if chev else ""

            panel = grant_subdiv.find("div", class_="panel")
            if not panel:
                continue

            for grant_card in panel.find_all("div", class_=["search-card-top", "search-card"]):
                title_tag = grant_card.find("a", class_="title-link")
                if not title_tag:
                    continue

                title = title_tag.get_text(strip=True)
                codigo_match = CODIGO_AVISO_REGEX.search(title)
                codigo_aviso = codigo_match.group(1) if codigo_match else None
                last_repub_number = None
                last_repub_date = None
                last_doc_link = None
                last_contact = None

                paragraphs = grant_card.find_all("p")
                data_inicio = next(
                    (DATE_REGEX.search(p.get_text(strip=True)).group(0)
                     for p in paragraphs
                     if DATE_REGEX.search(p.get_text(strip=True))),
                    None,
                )

                for detail in reversed(paragraphs):
                    text = detail.get_text(strip=True)
                    date_match = DATE_REGEX.search(text)

                    if last_repub_date is None and REPUB_REGEX.search(text):
                        label_match = NUMBER_REPUB_REGEX.search(text)
                        last_repub_number = label_match.group(0) if label_match else None
                        last_repub_date = date_match.group(0) if date_match else None

                    if last_doc_link is None and "ver documentação" in text.lower():
                        link = detail.find("a")
                        last_doc_link = link["href"] if link else None

                    if last_contact is None and "contacto para informações" in text.lower():
                        link = detail.find("a")
                        last_contact = link.get_text(strip=True) if link else None

                all_data.append({
                    "origem": "PRR",
                    "tipo": grants_type,
                    "subtitulo": grants_subtitle,
                    "titulo": title,
                    "codigo_aviso": codigo_aviso,
                    "data_inicio": data_inicio,
                    "data_publicacao": data_inicio,
                    "ultima_republicacao": last_repub_number, 
                    "data_ultima_republicacao": last_repub_date,
                    "contacto": last_contact,
                    "ultimo_aviso": last_doc_link,
                })
