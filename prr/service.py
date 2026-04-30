from .scraper import scrape_prr_web
from .converter import download_document


def scrape_prr() -> list[dict]:
    all_data = scrape_prr_web()

    """for aviso in all_data:
        if aviso.get("ultimo_aviso"):
            all_data = aviso["ultimo_aviso"]
            download_document(aviso["ultimo_aviso"])"""

    return all_data
