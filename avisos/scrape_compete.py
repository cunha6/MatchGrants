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

_AVISO_URL_RE = re.compile(
    r"^https?://(?:www\.)?compete2030\.gov\.pt/avisos/[^/?#]+/?$"
)


def scrape_compete2030_web() -> list[dict]:
    all_data: list[dict] = []
    scraped_urls: set[str] = set()
    page = 1
    total_pages: int | None = None

    while True:
        list_url = f"{LIST_URL}?per_page={DEFAULT_PER_PAGE}&status=1,3&page={page}"
        html = _fetch(list_url)
        if not html:
            break

        if total_pages is None:
            total_pages = max(_get_total_pages(html), page)

        for grant in _parse_listing(html):
            url = grant["url"]
            if url in scraped_urls:
                continue
            if SKIP_STATUSES.intersection(grant["estados"]):
                continue

            scraped_urls.add(url)
            detail_html = _fetch(url)
            if not detail_html:
                continue

            detail = _parse_detail(detail_html, url)
            all_data.append({**grant, **detail})
            time.sleep(REQUEST_DELAY)

        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    return all_data


def _fetch(url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            if not r.text:
                raise ValueError("Empty response")
            return r.text
        except (requests.RequestException, ValueError):
            if attempt < retries:
                time.sleep(3 * attempt)
    return None


def _parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results = []

    for article in soup.find_all("article", class_=re.compile(r"row.*g-0|border-bottom")):
        link = article.find("a", href=_AVISO_URL_RE)
        if not link:
            continue

        url = link["href"].rstrip("/") + "/"
        if url in seen:
            continue
        seen.add(url)

        title_el = link.find("h3", class_="card-title") or link
        title = title_el.get_text(" ", strip=True)

        status = _badges(article)

        reference_element = article.find("h4", class_=re.compile(r"fw-normal"))
        reference = reference_element.get_text(strip=True) if reference_element else ""

        deadline = ""
        candidature = article.find("div", class_=re.compile(r"col-md-3|col-3"))
        if candidature:
            date_candidature = candidature.find("p", class_=re.compile(r"fw-bold"))
            if date_candidature:
                deadline = date_candidature.get_text(strip=True)

        results.append({
            "url": url,
            "titulo": title,
            "estados": status,
            "codigo_aviso": reference,
            "fim_candidatura": deadline,
        })
    return results


def _parse_detail(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    data: dict = {"url": url}
    data["origem"] = "Compete2030"

    main_element = soup.find("main")
    div_date = main_element.find("div", class_="date-badges")

    publication_date = ""
    if div_date:
        span = div_date.find("span", class_=re.compile(r"text-muted"))
        publication_date = _clean(span.get_text())
    data["data_publicacao"] = _date_normalize(publication_date)

    data["data_inicio"] = ""
    for p in soup.find_all("p"):
        t = _clean(p.get_text())
        if "candidatura" in t.lower():
            m = re.search(r"\d{2}/\d{2}/\d{4}", t)
            if m:
                data["data_inicio"] = m.group(0)
                break

    data["fases"] = _parse_fases(main_element)

    info: dict[str, str] = {}
    if main_element:
        for content in main_element.find_all("div", class_=re.compile(r"col-md-6|col-12")):
            content_p = content.find_all("p", recursive=False)
            i = 0
            while i < len(content_p) - 1:
                title_p = content_p[i]
                value_p = content_p[i + 1]
                is_title = "mb-0" in title_p.get("class", [])
                is_data = "fw-bold" in value_p.get("class", [])
                if is_title and is_data:
                    t = _clean(title_p.get_text())
                    v = _clean(value_p.get_text())
                    if t and v:
                        info[t] = v
                    i += 2
                else:
                    i += 1
    data["entidades_beneficiarias"] = info.get("Entidades Beneficiárias", "")

    fin_rows = []
    if main_element:
        for table in main_element.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [_clean(c.get_text()) for c in rows[0].find_all("td")]
            for row in rows[1:]:
                cells = [_clean(c.get_text()) for c in row.find_all("td")]
                if any(cells) and not any("global" in c.lower() for c in cells):
                    row_dict = {headers[i]: cells[i] if i < len(cells) else "" for i in range(len(headers))}
                    fin_rows.append(_parse_dotacao_row(row_dict))
    data["tabela_financeira"] = fin_rows

    data["dotacao_global"] = _sum_dotacoes(fin_rows)

    docs_ul = None
    for h2 in soup.find_all("h2"):
        if _clean(h2.get_text()).lower() == "documentos":
            docs_ul = h2.find_next_sibling("ul")
            break

    docs = []
    if docs_ul:
        _date_in_txt = re.compile(r"(\d{4})[/-](\d{2})[/-](\d{2})")
        _repub_re = re.compile(r"republic", re.I)
        _aviso_re = re.compile(r"\baviso\b", re.I)
        seen_docs: set[str] = set()

        for a in docs_ul.find_all("a", href=True):
            href = a["href"]
            txt = _clean(a.get_text())
            full = href if href.startswith("http") else BASE_URL + href
            if full in seen_docs:
                continue
            seen_docs.add(full)
            docs.append({"nome": txt, "url": full})

        def _doc_order(doc):
            t = doc["nome"]
            if _repub_re.search(t):
                m = _date_in_txt.search(t)
                if m:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    return (0, -y, -mo, -d)
                return (0, 0, 0, 0)
            if _aviso_re.search(t):
                return (1, 0, 0, 0)
            return (2, 0, 0, 0)

        docs.sort(key=_doc_order)

    data["documentos"] = docs
    data["ultimo_aviso"] = None
    if docs:
        info = _pdf_info(docs[0]["url"])
        if info["paginas"] > 5 and info["natureza"] != "convite":
            data["ultimo_aviso"] = {"nome": docs[0]["nome"], "url": docs[0]["url"]}
        data["natureza_aviso"] = info["natureza"]

    return data


def _extract_date_hour(p_el) -> str:
    if not p_el:
        return ""
    bold = p_el.find("span", class_="fw-bold")
    date = _clean(bold.get_text()) if bold else ""
    hour = ""
    for span in p_el.find_all("span"):
        t = _clean(span.get_text())
        if re.search(r"\d{1,2}[h:]\d{2}", t):
            hour = " " + t.strip(", ")
            break
    return date + hour


def _parse_fases(main_element: Tag) -> list[dict]:
    fases = []

    for fase_div in main_element.find_all("div", class_="fases"):
        top_fase = fase_div.find("div", class_="fase-top")
        name = ""
        if top_fase:
            name_p = top_fase.find("p")
            name = _clean(name_p.get_text()) if name_p else ""

        fases.append({
            "nome": name,
            "data_inicio": _extract_date_hour(fase_div.find("p", class_="fase-start")),
            "data_fim": _extract_date_hour(fase_div.find("p", class_="fase-end")),
        })

    if not fases:
        btn = main_element.find("button", class_="btn-calendar")
        if btn:
            start_date = btn.get("data-date", "")
            end_date = btn.get("data-date_end", "")
            end_hour = ""
            for mod in main_element.find_all("div", class_="module"):
                h = mod.find(["h2", "h3"])
                if h and "candidatura" in h.get_text(strip=True).lower():
                    m = re.search(r"\((\d{1,2})\s*hora", _clean(mod.get_text()), re.I)
                    if m:
                        end_hour = f" {m.group(1)}:00h"
                    break
            fases.append({
                "nome": "Candidatura",
                "data_inicio": start_date,
                "data_fim": end_date + end_hour,
            })

    return fases


def _badges(el: Tag) -> list[str]:
    resultados = []
    for b in el.find_all("div", class_="badge"):
        texto = b.get_text(strip=True)
        if texto in ALL_STATUSES:
            resultados.append(texto)
    return resultados


_MESES = {
    "janeiro": "01", "fevereiro": "02", "março": "03", "abril": "04",
    "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
    "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12",
}


def _date_normalize(texto: str) -> str:
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+(\d{4})", texto, re.IGNORECASE)
    if m:
        dia, mes_str, ano = m.group(1), m.group(2).lower(), m.group(3)
        mes = _MESES.get(mes_str)
        if mes:
            return f"{dia.zfill(2)}/{mes}/{ano}"
    return texto


def _sum_dotacoes(fin_rows: list[dict]) -> str:
    total = 0
    for row in fin_rows:
        val = re.sub(r"[€\s]", "", row.get("dotacao", "")).replace(".", "").replace(",", ".")
        try:
            total += float(val)
        except ValueError:
            pass
    if not total:
        return ""
    return f"{int(total):,}".replace(",", ".") + "€"


_DOTACAO_KEY_RE = re.compile(r"dota[çc][aã]o.*dispon[íi]vel", re.IGNORECASE)
_DOTACAO_VAL_RE = re.compile(r"^(.*?)\s*[–—-]\s*(.+)$")


def _parse_dotacao_row(row_dict: dict) -> dict:
    for key in list(row_dict.keys()):
        if _DOTACAO_KEY_RE.search(key):
            val = row_dict.pop(key)
            m = _DOTACAO_VAL_RE.match(val)
            if m:
                row_dict["nome"] = m.group(1).strip()
                row_dict["dotacao"] = m.group(2).strip()
            else:
                row_dict["dotacao"] = val
            break
    return row_dict


def _pdf_info(url: str) -> dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        reader = PdfReader(io.BytesIO(r.content))
        text = " ".join(p.extract_text() or "" for p in reader.pages[:3]).lower()
        natureza = "convite" if "convite" in text else None
        return {"paginas": len(reader.pages), "natureza": natureza}
    except Exception:
        return {"paginas": 0, "natureza": None}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _get_total_pages(html: str) -> int:
    nums = re.findall(r"[?&]page=(\d+)", html)
    return max((int(n) for n in nums), default=1)
