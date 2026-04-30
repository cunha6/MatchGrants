from .scraper import scrape_portugal2030_web
from .converter import download_document


def scrape_portugal2030() -> list[dict]:
    all_data = scrape_portugal2030_web()

    """for aviso in all_data:
        if aviso.get("ultimo_aviso"):
            all_data = aviso
            download_document(aviso["ultimo_aviso"]["url"])"""

    return all_data
