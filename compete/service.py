"""
Serviço de scraping para o COMPETE 2030 (compete2030.gov.pt).

Recolhe todos os avisos com estado Aberto, Planeado ou Atualizado,
excluindo os Fechados, e devolve uma lista de dicionários com todos
os detalhes de cada aviso.
"""

import re #regex
import time #sleep
import requests #pedidos HTTP
from bs4 import BeautifulSoup, Tag #WebScraping

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

# URLs deos detalhes dos avisos: https://compete2030.gov.pt/avisos/detalhe/
_AVISO_URL_RE = re.compile(
    r"^https?://(?:www\.)?compete2030\.gov\.pt/avisos/[^/?#]+/?$"
)


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------
def scrape_compete2030() -> list[dict]:
    """
    Percorre todas as páginas de listagem, ignora avisos Fechados,
    e devolve a lista completa com todos os detalhes.
    """
    all_data: list[dict] = [] #todos os dados estruturados
    scraped_urls: set[str] = set() #url's já visitados
    page = 1
    total_pages: int | None = None #total de pagina é desconhecido

    while True:
        list_url = f"{LIST_URL}?per_page={DEFAULT_PER_PAGE}&status=1,3&page={page}"
        html = _fetch(list_url)
        if not html:
            break

        #Numero total de páginas
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

"""
Realiza 3 tentativas de aceder ao website, se falhar numa das tentativas, espera e volta a tentar mais tarde
Se falhar nas 3 tentativas return None
"""
def _fetch(url: str, retries: int = 3) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            #ligação ao link
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status() #status do request (200,500,404)
            r.encoding = "utf-8"

            print(f"Attempt {attempt}/{retries}")

            if not r.text:
                raise ValueError("Empty response")

            return r.text
        except (requests.RequestException, ValueError):
            if attempt < retries:
                time.sleep(3 * attempt)
    return None


"""
Scarping da página principal de avisos

<article class="row g-0 border-bottom mb-4 pt-4 pb-4">
    <div class="col-md-9 col-12">
        <div class="pe-md-4 pe-0">
            <div class="d-flex justify-content-between align-items-center">
                <div class="d-flex flex-wrap align-items-center mb-3">
                    <!--span class="fs-sm text-muted">30 de Janeiro 2026</span>
                    <span class="fs-xs opacity-20 mx-3">|</span-->
                    <div class="d-inline-block date-badges">
                        <div class="badge border-success text-success fs-xs border mt-2">Aberto</div>
                        <div class="badge text-nav fs-xs border mt-2">Atualizado</div>
                    </div>
                </div>
                <div class="d-block d-md-none">
                    <div class="mt-n3 ms-n3 share-btns d-flex justify-content-end">
                        <a class="d-none" download="step-inovacao-produtiva-digital-e-biotecnologia_mpr-2026-3.ics" href=""></a>
                        <button data-date="2026-01-30 22:00" data-date_end="2026-04-30 17:00" type="button" class="btn btn-outline-secondary btn-icon btn-sm btn-odnoklassniki rounded-circle ms-3" data-bs-placement="bottom" data-bs-html="true" title="Download do ficheiro de calendário do evento (.ics)" data-target="10472"><i class="ai-calendar-plus"></i></button>
                        <!--button type="button" class="btn btn-outline-secondary btn-icon btn-sm btn-linkedin rounded-circle ms-3" data-bs-placement="bottom" data-bs-html="true" title="Partilhar link"><i class="ai-linkedin"></i></button-->
                    </div>
                </div>
            </div>
                <p class="fw-semibold h6">STEP - Plataforma de Tecnologias Estratégicas para a Europa</p>
                <a href="https://compete2030.gov.pt/avisos/step-inovacao-produtiva-digital-e-biotecnologia_mpr-2026-3/"><h3 class="card-title mb-2">STEP - Inovação Produtiva - Digital e Biotecnologia</h3></a>
                <h4 class="fw-normal h5">MPr-2026-3</h4>                                           
                <p class="d-sm-none d-md-block"></p>
            </div>
        </div>
        <div class="col-md-3 col-12">
            <div class="mt-n3 ms-n3 share-btns d-md-flex d-none justify-content-end">
                <a class="d-none" download="step-inovacao-produtiva-digital-e-biotecnologia_mpr-2026-3.ics" href=""></a>
                <button data-date="2026-01-30 22:00" data-date_end="2026-04-30 17:00" type="button" class="btn btn-outline-secondary btn-icon btn-sm btn-odnoklassniki rounded-circle mt-3 ms-3" data-bs-placement="bottom" data-bs-html="true" title="Download do ficheiro de calendário do evento (.ics)" data-target="10472"><i class="ai-calendar-plus"></i></button>
                <!--button type="button" class="btn btn-outline-secondary btn-icon btn-sm btn-linkedin rounded-circle mt-3 ms-3" data-bs-placement="bottom" data-bs-html="true" title="Partilhar link"><i class="ai-linkedin"></i></button-->
            </div>
            <div class="d-block mt-md-0 mt-2">
                <p class="text-md-end mt-4 mb-1">Candidaturas até</p>
                <p class="text-md-end mb-1 fw-bold h5 ">30/04/2026</p>
	        </div>
        </div>
    </div>
</article>
"""
def _parse_listing(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set() #url's que já processamos, evitando duplicados
    results = []

    #Procura tudo que for article
    for article in soup.find_all("article", class_=re.compile(r"row.*g-0|border-bottom")):
        link = article.find("a", href=_AVISO_URL_RE) #procura e guarda toda a tag <a> com href com o _AVISO_URL_RE
        if not link:
            continue

        url = link["href"].rstrip("/") + "/" #guarda apenas o url e garante que tem / no fim para funcionar com os avisos detalhados
        if url in seen:
            continue
        
        seen.add(url)

        title_el = link.find("h3", class_="card-title") or link #guarda o titulo do aviso ou a tag <a> completa
        title = title_el.get_text(" ", strip=True) #remove espaços desnecessários

        status = _badges(article) #mostrar apenas os avisos Abertos e Planeado

        reference_element = article.find("h4", class_=re.compile(r"fw-normal")) #codigo do Aviso
        reference = reference_element.get_text(strip=True) if reference_element else ""

        deadline = ""
        candidature = article.find("div", class_=re.compile(r"col-md-3|col-3"))
        if candidature:
            date_candidature = candidature.find("p", class_=re.compile(r"fw-bold"))
            if date_candidature:
                deadline = date_candidature.get_text(strip=True) #data final de candidatura

        results.append({
            "url": url,
            "titulo": title,
            "estados": status,
            "referencia": reference,
            "fim_candidatura": deadline,
        })
    return results

# ---------------------------------------------------------------------------
# Detalhe
# ---------------------------------------------------------------------------
def _parse_detail(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    data: dict = {"url": url}

    main_element = soup.find("main") #elementos que estão dentro do main
    # Estados (badges no cabeçalho do detalhe)
    div_date = main_element.find("div", class_="date-badges")

    # Data de publicação
    publication_date = ""
    
    if div_date:
        span = div_date.find("span", class_=re.compile(r"text-muted"))
        publication_date = _clean(span.get_text())
    data["data_publicacao"] = publication_date

    # Período de candidatura (texto geral do módulo, cobre avisos sem fases)
    data["periodo_candidatura"] = ""
    for p in soup.find_all("p"):
        t = _clean(p.get_text())
        if "candidatura" in t.lower() and re.search(r"\d{2}/\d{2}/\d{4}", t):  #regex para obter tag com data
            data["periodo_candidatura"] = t
            break

    # Fases de candidatura (se o aviso não tiver fases retorna lista vazia)
    data["fases"] = _parse_fases(main_element)

    # Grid de informação (âmbito, tipologia, beneficiários, etc.)
    # Estrutura: <p class="mb-0">Label</p> + <p class="mb-4 fw-bold">Valor</p>
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
                    title = _clean(title_p.get_text())
                    value = _clean(value_p.get_text())
                    if title and value:
                        info[title] = value
                    i += 2
                else:
                    i += 1
    data["info_geral"] = info

    # Secções de conteúdo (módulos)
    # Estrutura: <div class="module mb-4" id="module_N"><h2>...</h2><div>...</div></div>
    """sections: dict[str, str] = {}
    if main_element:
        for mod in main_element.find_all("div", class_="module"):
            h = mod.find(["h2", "h3"])
            if not h:
                continue
            title = _clean(h.get_text())
            texts = [_clean(el.get_text()) for el in mod.find_all(["p", "li"])]
            texts = [t for t in texts if t and t != title]
            if title:
                sections[title] = " ".join(texts)
    data["secções"] = sections"""

    # Tabela financeira
    """fin_rows = []
    if main_element:
        for table in main_element.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue #Se só tiver cabeçalho avança
            headers = [_clean(c.get_text()) for c in rows[0].find_all("td")]
            for row in rows[1:]:
                cells = [_clean(c.get_text()) for c in row.find_all("td")]
                if any(cells):
                    fin_rows.append(dict(zip(headers, cells)))
    data["tabela_financeira"] = fin_rows
    """
    fin_rows = []

    if main_element:
        for table in main_element.find_all("table"):
            rows = table.find_all("tr")

            if len(rows) < 2:
                continue  # só cabeçalho → ignora

            headers = [_clean(c.get_text()) for c in rows[0].find_all("td")]

            for row in rows[1:]:
                cells = [_clean(c.get_text()) for c in row.find_all("td")]

                if any(cells):
                    row_dict = {}
                    
                    for i in range(len(headers)):
                        if i < len(cells):
                            # Se existe uma célula correspondente, guarda o valor
                            row_dict[headers[i]] = cells[i]
                        else:
                            # Se a linha terminou antes dos cabeçalhos, guarda vazio
                            row_dict[headers[i]] = ""      
    data["tabela_financeira"] = fin_rows
    
    # Atualizações
    """updates = []
    if main_element:
        upd_div = main_element.find("div", class_="bg-updates")
        if upd_div:
            for entry in upd_div.find_all("div", class_=re.compile(r"d-block.*mb-3|mb-3.*d-block")):
                dp = entry.find("p", class_="fw-bold")
                date_str = _clean(dp.get_text()) if dp else ""
                if dp:
                    dp.extract()
                updates.append({"data": date_str, "descricao": _clean(entry.get_text())})
    data["atualizacoes"] = updates"""

    # Documentos — apenas o que está dentro do módulo com <h2> "Documentos"
    # Se não existir esse módulo, guarda lista vazia
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
            """Procura apenas Repblicações e Avisos"""
            #if not _repub_re.search(txt) and not _aviso_re.search(txt):
                #continue
            full = href if href.startswith("http") else BASE_URL + href
            if full in seen_docs:
                continue
            seen_docs.add(full)
            docs.append({"texto": txt, "url": full})
            
        # Republicação mais recente primeiro; se não houver, aviso primeiro
        def _doc_order(doc):
            t = doc["texto"]
            if _repub_re.search(t):
                m = _date_in_txt.search(t)
                if m:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    return (0, -y, -mo, -d)  # mais recente primeiro: 2026 > 2025, depois mês, depois dia
                return (0, 0, 0, 0)
            if _aviso_re.search(t):
                return (1, 0, 0, 0)
            return (2, 0, 0, 0)

        docs.sort(key=_doc_order)
    data["documentos"] = docs

    return data

# ---------------------------------------------------------------------------
# Fases de candidatura
# ---------------------------------------------------------------------------
"""
Extrai as fases de candidatura do timeline visual da página de detalhe.

<div class="d-block ps-2 pe-3 me-1 fases mb-4">
    <div class="d-flex ... fase-top">
        <p class="text-center mb-1">Fase 1</p>
        <button data-date="2025-07-31 00:00" data-date_end="2025-10-31 17:00" ...></button>
    </div>
        <p class="mb-0 fase-start"><span class="small">de</span> <span class="fw-bold">31/07/2025</span></p>
        <p class="mb-0 fase-end"><span class="small">até</span> <span class="fw-bold">31/10/2025</span><span>, 17:00h</span></p>
</div>
"""
def _parse_fases(main_element: Tag) -> list[dict]:
    fases = []

    # Caso 1: aviso com fases explícitas (div.fases)
    for fase_div in main_element.find_all("div", class_="fases"):
        top_fase = fase_div.find("div", class_="fase-top")
        name = ""
        if top_fase:
            name_p = top_fase.find("p")
            name = _clean(name_p.get_text()) if name_p else ""

        start_date = ""
        fase_start = fase_div.find("p", class_="fase-start")
        if fase_start:
            bold = fase_start.find("span", class_="fw-bold")
            start_date = _clean(bold.get_text()) if bold else ""

        end_date = ""
        end_hour = ""
        fase_end = fase_div.find("p", class_="fase-end")
        if fase_end:
            bold = fase_end.find("span", class_="fw-bold")
            end_date = _clean(bold.get_text()) if bold else ""
            for span in fase_end.find_all("span"):
                t = _clean(span.get_text())
                if re.search(r"\d{1,2}[h:]\d{2}", t):
                    end_hour = t.strip(", ")
                    break

        fases.append({
            "nome": name,
            "data_inicio": start_date,
            "data_fim": end_date,
            "hora_fim": end_hour,
        })

    # Caso 2: sem fases explícitas — usar o botão de calendário
    if not fases:
        btn = main_element.find("button", class_="btn-calendar")
        if btn:
            start_date = btn.get("data-date", "")
            end_date = btn.get("data-date_end", "")
            # Extrair hora do módulo "Período de candidatura" (ex: "17 horas")
            end_hour = ""
            for mod in main_element.find_all("div", class_="module"):
                h = mod.find(["h2", "h3"])
                if h and "candidatura" in h.get_text(strip=True).lower():
                    m = re.search(r"\((\d{1,2})\s*hora", _clean(mod.get_text()), re.I)
                    if m:
                        end_hour = f"{m.group(1)}:00h"
                    break
            fases.append({
                "nome": "Candidatura",
                "data_inicio": start_date,
                "data_fim": end_date,
                "hora_fim": end_hour,
            })
    return fases

def _badges(el: Tag) -> list[str]:
    resultados = []
    for b in el.find_all("div", class_="badge"): #procura todas as div que tem a class badge que é para obter o estado do aviso
        texto = b.get_text(strip=True)
        if texto in ALL_STATUSES:
            resultados.append(texto)
    return resultados

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip() #regex para retirar espaço/paragrafos que sobram e coloca apenas um espaço


"""
Obter o número máximo de páginas para pesquisar
"""
def _get_total_pages(html: str) -> int:
    nums = re.findall(r"[?&]page=(\d+)", html)
    return max((int(n) for n in nums), default=1)