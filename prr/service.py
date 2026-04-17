import re
import time
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://recuperarportugal.gov.pt"
LIST_URL = f"{BASE_URL}/candidaturas-prr/#1624904725473-df8f5226-5d16/"
REQUEST_DELAY = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
}

DATE_REGEX = re.compile(r"\d{2}/\d{2}/\d{4}")
REPUB_REGEX = re.compile(r"Republicação", re.IGNORECASE)
NUMBER_REPUB_REGEX = re.compile(r"(?:\d+[ªº.]*\s*)?Republicação", re.IGNORECASE)


def _get_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def scrape_prr() -> list[dict]:
    all_data: list[dict] = []
    print("A iniciar scraping do PRR...")

    driver = open_grants()
    try:
        scrape_grants(driver, all_data)
    finally:
        driver.quit()

    return all_data

def scrape_grants(driver: webdriver.Chrome, all_data: list[dict]):
    soup = BeautifulSoup(driver.page_source, "lxml")

    panels = soup.find_all("div", class_="vc_tta-panel", attrs={"data-vc-content": ".vc_tta-panel-body"}) #TUDO

    for type_panel in panels:
        grants_type = type_panel.find("span", class_="vc_tta-title-text").get_text(strip=True) #C01

        for grant_subdiv in type_panel.find_all("div", class_=["entidadespublicas","empresaspublicas", "ben", "accordion"]):
            grants_subtitle = grant_subdiv.find("div", id="prr-arrow-chev", class_="arrow-chev").get_text(strip=True) if grant_subdiv.find("div", id="prr-arrow-chev", class_="arrow-chev") else "" #C01.01

            panel = grant_subdiv.find("div", class_="panel")
            if not panel:
                continue

            for grant_card in panel.find_all("div", class_=["search-card-top", "search-card"]):
                title_tag = grant_card.find("a", class_="title-link")
                if not title_tag:
                    continue
                title = title_tag.get_text(strip=True)
                
                original_date = None
                last_repub_number = None
                last_repub_date = None
                last_doc_link = None
                last_contact = None

                for detail in reversed(grant_card.find_all("p")):
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

                    if date_match:
                        original_date = date_match.group(0)

                all_data.append({
                    "type": grants_type,
                    "subtitle": grants_subtitle,
                    "title": title,
                    "original_date": original_date,
                    "last_repub": last_repub_number,
                    "last_repub_date": last_repub_date,
                    "last_doc_link": last_doc_link,
                    "last_contact": last_contact,
                })
                print(f"Tipo: {grants_type} | Subtitulo: {grants_subtitle} | Titulo: {title} | Última Rep.: {last_repub_date} | Link: {last_doc_link} | Contacto: {last_contact}")

def open_grants() -> webdriver.Chrome:
    driver = _get_driver()
    driver.get(LIST_URL)
    wait = WebDriverWait(driver, 20)

    # Clicar no botão "Abertos"
    aberto_btn = wait.until(EC.element_to_be_clickable((By.ID, "aberto-btn")))
    aberto_btn.click()
    print("Clicou em 'Abertos'")
    time.sleep(1)

    # Clicar no botão "Pesquisar Avisos"
    pesquisar_btn = wait.until(EC.element_to_be_clickable((By.ID, "pesquisar-btn")))
    pesquisar_btn.click()
    print("Clicou em 'Pesquisar Avisos'")
    time.sleep(2)

    return driver