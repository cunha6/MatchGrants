import io
import re
import time
import requests
from bs4 import BeautifulSoup, Tag
from pypdf import PdfReader

BASE_URL = "https://compete2030.gov.pt"
LIST_URL = f"{BASE_URL}/avisos/"
DEFAULT_PER_PAGE = 24
REQUEST_DELAY = 1
ALL_STATUSES = {"Aberto", "Fechado", "Planeado", "Atualizado"}
SKIP_STATUSES = {"Fechado"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
}

_GRANT_URL_RE = re.compile(
    r"^https?://(?:www\.)?compete2030\.gov\.pt/avisos/[^/?#]+/?$"
)


def scrape_compete2030_web() -> list[dict]:
    scraped_grants: list[dict] = []
    scraped_grant_urls: set[str] = set()
    page_number = 1
    total_page_count: int | None = None

    while True:
        listing_url = f"{LIST_URL}?per_page={DEFAULT_PER_PAGE}&status=1,3&page={page_number}"
        listing_html = _fetch(listing_url)
        if not listing_html:
            break

        if total_page_count is None:
            total_page_count = max(_get_total_pages(listing_html), page_number)

        for grant in _parse_listing(listing_html):
            url = grant["url"]
            if url in scraped_grant_urls:
                continue
            if SKIP_STATUSES.intersection(grant["estados"]):
                continue

            scraped_grant_urls.add(url)
            detail_page_html = _fetch(url)
            if not detail_page_html:
                continue

            detail_fields = _parse_detail(detail_page_html, url)
            scraped_grants.append({**grant, **detail_fields})
            time.sleep(REQUEST_DELAY)

        if page_number >= total_page_count:
            break
        page_number += 1
        time.sleep(REQUEST_DELAY)

    return scraped_grants


def _fetch(url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            http_response = requests.get(url, headers=HEADERS, timeout=30)
            http_response.raise_for_status()
            http_response.encoding = "utf-8"
            if not http_response.text:
                raise ValueError("Empty response")
            return http_response.text
        except (requests.RequestException, ValueError):
            if attempt < retries:
                time.sleep(3 * attempt)
    return None


def _parse_listing(listing_html: str) -> list[dict]:
    parsed_page = BeautifulSoup(listing_html, "lxml")
    seen_grant_urls: set[str] = set()
    listed_grants = []

    for article in parsed_page.find_all("article", class_=re.compile(r"row.*g-0|border-bottom")):
        link = article.find("a", href=_GRANT_URL_RE)
        if not link:
            continue

        url = link["href"].rstrip("/") + "/"
        if url in seen_grant_urls:
            continue
        seen_grant_urls.add(url)

        title_element = link.find("h3", class_="card-title") or link
        title = title_element.get_text(" ", strip=True)

        status_labels = _status_badges(article)

        grant_code_element = article.find("h4", class_=re.compile(r"fw-normal"))
        grant_code = grant_code_element.get_text(strip=True) if grant_code_element else ""

        closing_date_text = ""
        deadline_column = article.find("div", class_=re.compile(r"col-md-3|col-3"))
        if deadline_column:
            deadline_paragraph = deadline_column.find("p", class_=re.compile(r"fw-bold"))
            if deadline_paragraph:
                closing_date_text = deadline_paragraph.get_text(strip=True)

        listed_grants.append({
            "url": url,
            "title": title,
            "estados": status_labels,
            "grant_code": grant_code,
            "closing_date": closing_date_text,
        })
    return listed_grants


# Ordenação dos documentos do aviso: republicações primeiro (a mais recente à cabeça, pela
# data no próprio nome), depois o aviso base, depois tudo o resto.
_DOC_DATE_RE = re.compile(r"(\d{4})[/-](\d{2})[/-](\d{2})")
_DOC_REPUB_RE = re.compile(r"republic", re.I)
_DOC_AVISO_RE = re.compile(r"\baviso\b", re.I)


def _extract_publication_date(main_element) -> str:
    """Data de publicação, do badge no topo. Vazio se o HTML do site mudar."""
    date_badge_container = main_element.find("div", class_="date-badges") if main_element else None
    if not date_badge_container:
        return ""
    muted_span = date_badge_container.find("span", class_=re.compile(r"text-muted"))
    return _clean(muted_span.get_text()) if muted_span else ""


def _extract_start_date(parsed_page) -> str:
    """Data de início das candidaturas: a 1ª data dd/mm/aaaa num parágrafo que fale de
    candidatura. Vazio se nenhum parágrafo a tiver."""
    for paragraph in parsed_page.find_all("p"):
        text_content = _clean(paragraph.get_text())
        if "candidatura" in text_content.lower():
            match = re.search(r"\d{2}/\d{2}/\d{4}", text_content)
            if match:
                return match.group(0)
    return ""


def _extract_info_pairs(main_element) -> dict[str, str]:
    """Pares título→valor da ficha do aviso. No HTML do site vêm como <p> CONSECUTIVOS: o
    título tem a classe `mb-0` e o valor `fw-bold`. Só conta quando ambos batem certo — daí
    o avanço de 2 em 2 (par consumido) ou de 1 em 1 (não era um par)."""
    info_pairs: dict[str, str] = {}
    if not main_element:
        return info_pairs
    for column in main_element.find_all("div", class_=re.compile(r"col-md-6|col-12")):
        column_paragraphs = column.find_all("p", recursive=False)
        index = 0
        while index < len(column_paragraphs) - 1:
            label_paragraph, value_paragraph = column_paragraphs[index], column_paragraphs[index + 1]
            is_title = "mb-0" in label_paragraph.get("class", [])
            is_value = "fw-bold" in value_paragraph.get("class", [])
            if is_title and is_value:
                text_content, value = _clean(label_paragraph.get_text()), _clean(value_paragraph.get_text())
                if text_content and value:
                    info_pairs[text_content] = value
                index += 2
            else:
                index += 1
    return info_pairs


def _extract_financial_rows(main_element) -> list:
    """Linhas da tabela de dotação. Ignora as linhas de total ("global"), que somariam duas
    vezes o mesmo dinheiro, e tabelas sem corpo (menos de 2 linhas)."""
    allocation_rows = []
    if not main_element:
        return allocation_rows
    for table in main_element.find_all("table"):
        table_rows = table.find_all("tr")
        if len(table_rows) < 2:
            continue
        header_labels = [_clean(cell.get_text()) for cell in table_rows[0].find_all("td")]
        for table_row in table_rows[1:]:
            cell_values = [_clean(cell.get_text()) for cell in table_row.find_all("td")]
            if any(cell_values) and not any("global" in cell.lower() for cell in cell_values):
                row_by_header = {header_labels[index]: cell_values[index] if index < len(cell_values) else "" for index in range(len(header_labels))}
                allocation_rows.append(_split_allocation_header(row_by_header))
    return allocation_rows


def _doc_order(document) -> tuple:
    """Chave de ordenação de um documento (ver _DOC_*_RE): republicação mais recente primeiro."""
    text_content = document["nome"]
    if _DOC_REPUB_RE.search(text_content):
        match = _DOC_DATE_RE.search(text_content)
        if match:
            return (0, -int(match.group(1)), -int(match.group(2)), -int(match.group(3)))
        return (0, 0, 0, 0)
    if _DOC_AVISO_RE.search(text_content):
        return (1, 0, 0, 0)
    return (2, 0, 0, 0)


def _extract_documents(parsed_page) -> list[dict]:
    """Documentos da secção "Documentos", deduplicados por URL e já ordenados."""
    documents_list = None
    for h2 in parsed_page.find_all("h2"):
        if _clean(h2.get_text()).lower() == "documentos":
            documents_list = h2.find_next_sibling("ul")
            break
    if not documents_list:
        return []

    documents, seen_document_urls = [], set()
    for link in documents_list.find_all("a", href=True):
        href = link["href"]
        absolute_url = href if href.startswith("http") else BASE_URL + href
        if absolute_url in seen_document_urls:
            continue
        seen_document_urls.add(absolute_url)
        documents.append({"nome": _clean(link.get_text()), "url": absolute_url})

    documents.sort(key=_doc_order)
    return documents


def _parse_detail(listing_html: str, url: str) -> dict:
    """Ficha completa de um aviso do Compete2030.

    Cada extração é tolerante a mudanças no HTML do site: sem o elemento esperado devolve
    vazio, em vez de rebentar o scrape inteiro com AttributeError.
    """
    parsed_page = BeautifulSoup(listing_html, "lxml")
    main_element = parsed_page.find("main")

    data: dict = {"url": url, "source": "Compete2030"}
    data["data_publicacao"] = _normalize_written_date(_extract_publication_date(main_element))
    data["data_inicio"] = _extract_start_date(parsed_page)
    data["phases"] = _parse_phases(main_element) if main_element else []
    data["entidades_beneficiarias"] = _extract_info_pairs(main_element).get(
        "Entidades Beneficiárias", "")

    allocation_rows = _extract_financial_rows(main_element)
    data["tabela_financeira"] = allocation_rows
    data["dotacao_global"] = _sum_allocations(allocation_rows)

    documents = _extract_documents(parsed_page)
    data["documentos"] = documents
    # O aviso canónico é o 1º documento (já ordenado), mas só se for mesmo um aviso: um PDF
    # curto ou de natureza "convite" não serve para extração.
    data["latest_notice"] = None
    if documents:
        info_pairs = _pdf_info(documents[0]["url"])
        if info_pairs["paginas"] > 5 and info_pairs["natureza"] != "convite":
            data["latest_notice"] = {"nome": documents[0]["nome"], "url": documents[0]["url"]}
        data["notice_nature"] = info_pairs["natureza"]

    return data


def _extract_date_with_hour(paragraph) -> str:
    if not paragraph:
        return ""
    bold_span = paragraph.find("span", class_="fw-bold")
    date = _clean(bold_span.get_text()) if bold_span else ""
    hour_text = ""
    for muted_span in paragraph.find_all("span"):
        text_content = _clean(muted_span.get_text())
        if re.search(r"\d{1,2}[h:]\d{2}", text_content):
            hour_text = " " + text_content.strip(", ")
            break
    return date + hour_text


def _parse_phases(main_element: Tag) -> list[dict]:
    phases = []

    for phase_block in main_element.find_all("div", class_="phases"):
        phase_header = phase_block.find("div", class_="fase-top")
        phase_name = ""
        if phase_header:
            name_paragraph = phase_header.find("p")
            phase_name = _clean(name_paragraph.get_text()) if name_paragraph else ""

        phases.append({
            "nome": phase_name,
            "data_inicio": _extract_date_with_hour(phase_block.find("p", class_="fase-start")),
            "data_fim": _extract_date_with_hour(phase_block.find("p", class_="fase-end")),
        })

    if not phases:
        calendar_button = main_element.find("button", class_="btn-calendar")
        if calendar_button:
            start_date = calendar_button.get("data-date", "")
            end_date = calendar_button.get("data-date_end", "")
            end_hour = ""
            for module_block in main_element.find_all("div", class_="module"):
                heading = module_block.find(["h2", "h3"])
                if heading and "candidatura" in heading.get_text(strip=True).lower():
                    match = re.search(r"\((\d{1,2})\s*hora", _clean(module_block.get_text()), re.I)
                    if match:
                        end_hour = f" {match.group(1)}:00h"
                    break
            phases.append({
                "nome": "Candidatura",
                "data_inicio": start_date,
                "data_fim": end_date + end_hour,
            })

    return phases


def _status_badges(element: Tag) -> list[str]:
    matched_labels = []
    for badge in element.find_all("div", class_="badge"):
        text = badge.get_text(strip=True)
        if text in ALL_STATUSES:
            matched_labels.append(text)
    return matched_labels


_MONTH_NUMBER_BY_NAME = {
    "janeiro": "01", "fevereiro": "02", "março": "03", "abril": "04",
    "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
    "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
}


def _normalize_written_date(text: str) -> str:
    # "(?:\s+de)?" aceita as duas formas: "10 de junho 2026" e "10 de junho DE 2026".
    match = re.search(r"(\d{1,2})\s+de\s+(\w+)(?:\s+de)?\s+(\d{4})", text, re.IGNORECASE)
    if match:
        day, month_name, year = match.group(1), match.group(2).lower(), match.group(3)
        month_number = _MONTH_NUMBER_BY_NAME.get(month_name)
        if month_number:
            return f"{day.zfill(2)}/{month_number}/{year}"
    return text


def _sum_allocations(allocation_rows: list[dict]) -> str:
    total_amount = 0
    for table_row in allocation_rows:
        raw_amount = re.sub(r"[€\s]", "", table_row.get("dotacao", "")).replace(".", "").replace(",", ".")
        try:
            total_amount += float(raw_amount)
        except ValueError:
            pass
    if not total_amount:
        return ""
    return f"{int(total_amount):,}".replace(",", ".") + "€"


_ALLOCATION_HEADER_RE = re.compile(r"dota[çc][aã]o.*dispon[íi]vel", re.IGNORECASE)
_ALLOCATION_VALUE_RE = re.compile(r"^(.*?)\s*[–—-]\s*(.+)$")


def _split_allocation_header(row_by_header: dict) -> dict:
    for header_label in list(row_by_header.keys()):
        if _ALLOCATION_HEADER_RE.search(header_label):
            raw_amount = row_by_header.pop(header_label)
            match = _ALLOCATION_VALUE_RE.match(raw_amount)
            if match:
                row_by_header["nome"] = match.group(1).strip()
                row_by_header["dotacao"] = match.group(2).strip()
            else:
                row_by_header["dotacao"] = raw_amount
            break
    return row_by_header


def _pdf_info(url: str) -> dict:
    try:
        http_response = requests.get(url, headers=HEADERS, timeout=30)
        http_response.raise_for_status()
        pdf_reader = PdfReader(io.BytesIO(http_response.column))
        text = " ".join(paragraph.extract_text() or "" for paragraph in pdf_reader.pages[:3]).lower()
        nature = "convite" if "convite" in text else None
        return {"paginas": len(pdf_reader.pages), "natureza": nature}
    except Exception:
        return {"paginas": 0, "natureza": None}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _get_total_pages(listing_html: str) -> int:
    page_numbers = re.findall(r"[?&]page=(\d+)", listing_html)
    return max((int(page_number_text) for page_number_text in page_numbers), default=1)
