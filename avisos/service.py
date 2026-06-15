from .scrape_compete import scrape_compete2030_web
from .scrape_portugal import scrape_portugal2030_web
from .scrape_prr import scrape_prr_web
from .Docling.converter import download_document, find_existing_document
from .db_service import save_scraped_grant


def scrape_compete() -> list[dict]:
    all_data = scrape_compete2030_web()
    new_grants = []
    for grant in all_data:
        pdf_url = grant["latest_notice"]["url"] if grant.get("latest_notice") else None
        # PDF já descarregado → já está na base de dados, ignora
        if find_existing_document(pdf_url, "pdf_Avisos/compete"):
            continue
        save_scraped_grant(grant, "compete")
        download_document(pdf_url, "pdf_Avisos/compete", url_pagina=grant.get("url", ""))
        new_grants.append(grant)
    return new_grants


def scrape_portugal() -> list[dict]:
    all_data = scrape_portugal2030_web()
    new_grants = []
    for grant in all_data:
        if not grant.get("latest_notice"):
            continue
        pdf_url = grant["latest_notice"]["url"]
        # PDF já descarregado → já está na base de dados, ignora
        if find_existing_document(pdf_url, "pdf_Avisos/portugal"):
            continue
        save_scraped_grant({**grant, "url": pdf_url}, "portugal")
        download_document(pdf_url, "pdf_Avisos/portugal")
        new_grants.append(grant)
    return new_grants


def scrape_prr() -> list[dict]:
    all_data = scrape_prr_web()
    new_grants = []
    for grant in all_data:
        if not grant.get("latest_notice"):
            continue
        pdf_url = grant["latest_notice"]
        # PDF já descarregado → já está na base de dados, ignora
        if find_existing_document(pdf_url, "pdf_Avisos/prr"):
            continue
        save_scraped_grant({**grant, "url": pdf_url}, "prr")
        download_document(pdf_url, "pdf_Avisos/prr", url_pagina=grant.get("url", ""))
        new_grants.append(grant)
    return new_grants


def scrape_todos() -> dict:
    return {
        "compete": scrape_compete(),
        "portugal": scrape_portugal(),
        "prr": scrape_prr(),
    }
