from .scraper import scrape_compete2030_web
from .converter import download_and_convert


def scrape_compete2030() -> list[dict]:
    all_data = scrape_compete2030_web()

    for aviso in all_data:
        url = aviso["ultimo_aviso"]["url"] if aviso.get("ultimo_aviso") else None
        #aviso["conteudo_aviso"] = download_and_convert(url)

    return all_data
