from .scrape_compete import scrape_compete2030_web
from .scrape_portugal import scrape_portugal2030_web
from .scrape_prr import scrape_prr_web
from .Docling.converter import download_document
from .db_service import guardar_aviso_scraping


def scrape_compete() -> list[dict]:
    all_data = scrape_compete2030_web()
    for aviso in all_data:
        guardar_aviso_scraping(aviso, "compete")
        pdf_url = aviso["ultimo_aviso"]["url"] if aviso.get("ultimo_aviso") else None
        download_document(pdf_url, "pdf_Avisos/compete", url_pagina=aviso.get("url", ""))
    return all_data


def scrape_portugal() -> list[dict]:
    all_data = scrape_portugal2030_web()
    filtered = [aviso for aviso in all_data if aviso.get("ultimo_aviso")]
    for aviso in filtered:
        pdf_url = aviso["ultimo_aviso"]["url"]
        guardar_aviso_scraping({**aviso, "url": pdf_url}, "portugal")
        download_document(pdf_url, "pdf_Avisos/portugal")
    return filtered


def scrape_prr() -> list[dict]:
    all_data = scrape_prr_web()
    for aviso in all_data:
        if aviso.get("ultimo_aviso"):
            guardar_aviso_scraping(aviso, "prr")
            download_document(aviso["ultimo_aviso"], "pdf_Avisos/prr", url_pagina=aviso.get("url", ""))
    return all_data


def scrape_todos() -> dict:
    return {
        "compete": scrape_compete(),
        "portugal": scrape_portugal(),
        "prr": scrape_prr(),
    }
