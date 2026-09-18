"""Sincronização das calls do portal Funding & Tenders (SEDIA) da Comissão Europeia.

Fluxo do sync (`sync_calls`):
  1. faceta `/facet` → catálogos `ProgrammeType` (frameworkProgramme) e `CallType` (type);
  2. `/search` paginado (pageSize=100, pageNumber=1,2,3...) até vir uma página sem results;
  3. upsert de cada call por `reference`, atualizando SÓ os campos que mudaram.

Os catálogos são sincronizados ANTES das calls porque cada call referencia o programa e o
tipo por código; sem o catálogo carregado a FK ficava a null e o filtro por programa não
tinha por onde pegar.

Notas sobre a API (verificadas contra a resposta real, não presumidas):
  * o corpo do /search é MULTIPART (form-data) com cada campo em application/json — não é
    um POST JSON; um JSON simples devolve 0 resultados;
  * toda a metadata vem embrulhada em listas (`"status": ["31094502"]`), mesmo quando só
    pode ter um valor — daí `_first()`;
  * `deadlineDate` é genuinamente multi-valor (submissão em cascata);
  * o fim da paginação é uma página com `results: []` — `totalResults` existe mas a API
    repete linhas, por isso contar linhas contra o total nunca fecha certo;
  * a ordenação do pedido decide se a paginação é sequer completa — ver `SORT`.
"""

import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, unquote

import requests
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone as django_timezone

from .models import Call, CallType, CodeLabel, ProgrammeType

logger = logging.getLogger(__name__)

# "%C5" e afins — marca de um nome URL-encoded vindo das facetas (ver _clean_label).
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")

SEARCH_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"
FACET_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/facet"
API_KEY = "SEDIA"

# Tipos importados: 1 Grant, 2 Calls for proposals, 8 Cascade funding calls.
# O tipo 0 (Tender) está DE FORA por decisão de produto: a sua metadata (lots, CPV,
# entidade adjudicante) não tem nada a ver com a das restantes e não cabe no modelo `Call`.
IMPORTED_TYPES = ["1", "2", "8"]
# Estados: Forthcoming (ainda não abriu) e Open (a decorrer). Os fechados não interessam.
# Só Forthcoming e Open — o pedido original é para acompanhar calls ainda não fechadas.
# STATUS_CLOSED existe no modelo (choices), mas NÃO entra aqui: importá-lo trouxe, num único
# sync, 5887 calls fechadas para a BD (de 1339 para 6811 linhas) — muito acima do universo
# que esta app foi desenhada para servir.
IMPORTED_STATUSES = [Call.STATUS_FORTHCOMING, Call.STATUS_OPEN]

# Dimensões que a API devolve só como código e cujos nomes vêm do /facet (ver CodeLabel).
# `typeOfMGAs` fica de fora de propósito: a faceta devolve o próprio número como "nome",
# portanto traduzi-lo não acrescentava nada.
CODE_FACETS = [
    "destination", "destinationGroup", "mission", "missionGroup", "geographicalZones",
    "contractType", "programmeDivision", "specificObjective", "programmeDivisionProspect",
]

PAGE_SIZE = 100

# Ordenação da paginação. NÃO é `sortStatus` (o do portal) por um motivo medido: esse campo só
# tem dois valores (Open/Forthcoming), logo mais de mil linhas ficam empatadas e a API
# desempata-as de forma arbitrária a cada pedido. Entre páginas consecutivas as linhas
# deslocavam-se, umas repetiam-se e outras escapavam: dois varrimentos seguidos devolviam
# 1341 linhas mas conjuntos DIFERENTES (~25 calls que só apareciam num deles) e só ~1314
# calls distintas. Cada sync "descobria" dezenas de calls que já lá deviam estar.
# `identifier` é quase único, por isso desempata: 3 varrimentos seguidos deram exatamente as
# mesmas 1339 calls, e o mesmo com pageSize=200.
SORT = {"field": "identifier", "order": "ASC"}
# Travão de segurança: a paginação para sozinha na 1.ª página vazia, mas se a API passar a
# devolver sempre resultados (ou a ignorar o pageNumber) isto evita um ciclo infinito a
# martelar a Comissão Europeia. 200 páginas x 100 = 20 000 calls, muito acima das ~1 300 reais.
MAX_PAGES = 200
TIMEOUT = 90  # segundos — o /facet devolve vários MB


class CallsSyncError(Exception):
    """Falha a falar com a API do Funding & Tenders (rede, HTTP != 200, JSON inválido)."""


# --------------------------------------------------------------------------------------
# Normalização da resposta
# --------------------------------------------------------------------------------------

def _first(value, default=""):
    """Primeiro elemento de `value` quando a API o embrulha numa lista.

    A metadata do SEDIA devolve QUASE tudo como lista, mesmo campos de valor único
    (`"type": ["1"]`). Sem isto, o `status` guardado ficava a literal `['31094502']` e
    nenhum filtro batia certo.
    """
    if isinstance(value, list):
        return value[0] if value else default
    return default if value is None else value


def _as_list(value) -> list:
    """`value` como lista, tolerando campo ausente, valor único ou lista."""
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, list) else [value]


def _field(result: dict, key: str):
    """Valor cru de `key` num resultado — do NÍVEL DE TOPO ou de `metadata`, o que existir.

    A API normalmente devolve `{..., "metadata": {"status": [...], ...}}`, mas 5 das 1339
    calls verificadas (tipo 8, "COMPETITIVE_CALL") não trazem a chave "metadata" de todo — os
    mesmos campos vêm soltos no nível de topo do resultado. Sem este fallback, essas 5 calls
    ficavam com todas as colunas cruas vazias, embora os dados estivessem lá.

    Quando a MESMA chave existe nos dois níveis com valores diferentes (não acontece nos
    dados verificados, mas a API não garante o contrário), o nível de topo tem prioridade —
    é o mais próximo do "resultado" que a API devolve.
    """
    if key in result and key != "metadata":
        return result[key]
    return (result.get("metadata") or {}).get(key)


def _parse_datetime(value):
    """Datetime (UTC) a partir dos formatos que a API usa, ou None.

    Aparecem dois: "2026-12-08T00:00:00.000+0000" (a maioria) e, em alguns campos,
    "2024-09-27+02:00 Europe/Brussels". O `common.dates.parse_date` do projeto devolveria
    só a data — aqui a HORA importa (um prazo é às 17:00, não ao fim do dia), por isso a
    conversão é feita localmente.
    """
    if not value:
        return None
    text = str(value).strip()
    # "+0000" -> "+00:00", que é o que o fromisoformat aceita
    if len(text) >= 5 and (text[-5] in "+-") and text[-5:].replace("+", "").replace("-", "").isdigit():
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Último recurso: só a parte da data (trata "2024-09-27+02:00 Europe/Brussels").
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            logger.debug("Data não reconhecida na API de calls: %r", value)
            return None
    # USE_TZ está ligado: um datetime naive rebentava com warnings e comparações erradas.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _json_field(value):
    """Campo que a API manda como STRING com JSON lá dentro (`budgetOverview`, `actions`).

    Devolve o objeto já descodificado, ou None se não for JSON. Sem isto, o front-end
    teria de fazer JSON.parse de um campo que já vem dentro de JSON.
    """
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _boolean(value):
    """"true"/"false" da API para bool; None quando o campo não vem."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return True if text == "true" else False if text == "false" else None


def _decimal(value):
    """Decimal a partir de um número ou string numérica da API; None se não der."""
    if value in (None, "", []):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _budget(metadata: dict, identifier: str) -> dict:
    """Orçamento DESTA call.

    A API publica o orçamento de DUAS formas, conforme o tipo de call, e nunca as duas ao
    mesmo tempo (verificado: 0 sobreposições em 1339 calls):

      * Grants (tipo 1) → `budgetOverview`, uma STRING com JSON que descreve a call-mãe
        INTEIRA, todos os topics irmãos incluídos. A entrada desta call é a que tem `action`
        a começar pelo seu `identifier`; somar o resto daria o orçamento do programa todo
        (medido: 4,6 mil milhões numa call de 17 M€).
      * Cascade funding (tipo 8) e Calls for proposals (tipo 2) → `budget`, uma string com o
        valor já pronto. São 199 calls, todas com valor — ler só o `budgetOverview` deixava-as
        a zero.

    A moeda é sempre EUR (o campo `currency` só aparece no tipo 2, sempre a "EUR").
    """
    empty = {"budget_total": None, "min_contribution": None,
             "max_contribution": None, "expected_grants": None}
    overview = _json_field(_first(metadata.get("budgetOverview")))
    if not isinstance(overview, dict) or not identifier:
        # Sem budgetOverview: o valor simples dos tipos 2 e 8, se existir. O `or None`
        # descarta o 0 de quem não o publicou (1 call), para não mostrar "0 €" como se
        # fosse um orçamento real.
        return {**empty, "budget_total": _decimal(_first(metadata.get("budget"))) or None}
    for entries in (overview.get("budgetTopicActionMap") or {}).values():
        for entry in entries or []:
            action = str(entry.get("action") or "")
            # "<identifier> - <tipo de ação>": o espaço evita que HORIZON-X-01 apanhe
            # o orçamento de HORIZON-X-01-02.
            if not action.startswith(f"{identifier} "):
                continue
            total = sum(
                amount for amount in (
                    _decimal(year_value)
                    for year_value in (entry.get("budgetYearMap") or {}).values()
                ) if amount is not None
            )
            return {
                "budget_total": total or None,
                "min_contribution": _decimal(entry.get("minContribution")),
                "max_contribution": _decimal(entry.get("maxContribution")),
                "expected_grants": entry.get("expectedGrants") or None,
            }
    return empty


def _next_deadline(deadlines: list):
    """Prazo mais próximo AINDA no futuro; se já passaram todos, o último.

    136 das 1341 calls verificadas têm vários prazos (submissão em cascata). Guardar o
    primeiro da lista dava uma ordenação errada — a call apareceria como expirada enquanto
    ainda aceita candidaturas na ronda seguinte.
    """
    parsed = sorted(d for d in (_parse_datetime(v) for v in deadlines) if d)
    if not parsed:
        return None
    now = django_timezone.now()
    return next((d for d in parsed if d >= now), parsed[-1])


# --------------------------------------------------------------------------------------
# Chamadas à API
# --------------------------------------------------------------------------------------

def _fetch_page(page_number: int) -> list:
    """Uma página do /search. Devolve a lista de results (vazia no fim da paginação)."""
    query = {
        "bool": {
            "must": [
                {"terms": {"type": IMPORTED_TYPES}},
                {"terms": {"status": IMPORTED_STATUSES}},
            ]
        }
    }
    # Cada campo vai como parte multipart com content-type application/json — é o que a API
    # exige. (None, valor, tipo) é a forma do `requests` de mandar um campo sem ficheiro.
    files = {
        "query": (None, json.dumps(query), "application/json"),
        "languages": (None, json.dumps(["en"]), "application/json"),
        "sort": (None, json.dumps(SORT), "application/json"),
    }
    params = {
        "apiKey": API_KEY,
        "text": "***",
        "pageSize": PAGE_SIZE,
        "pageNumber": page_number,
    }
    try:
        response = requests.post(SEARCH_URL, params=params, files=files, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CallsSyncError(f"Pedido à API de calls falhou (página {page_number}): {exc}") from exc
    return payload.get("results") or []


def _fetch_facets(facet_fields: list[str]) -> dict[str, list]:
    """Facetas pedidas, por rawName → lista de values.

    O /facet devolve MUITO mais facetas do que as pedidas (29 na verificação, ~4,7 MB) e
    chega a repetir o mesmo rawName em objetos diferentes; por isso acumula-se por rawName
    em vez de assumir uma correspondência 1-para-1 com o que foi pedido.
    """
    body = {
        "language": "en",
        "query": {"bool": {"must": [{"terms": {"type": IMPORTED_TYPES}}]}},
        "facetFields": facet_fields,
    }
    try:
        response = requests.post(
            FACET_URL,
            params={"apiKey": API_KEY, "text": "***"},
            json=body,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise CallsSyncError(f"Pedido às facetas de calls falhou: {exc}") from exc

    grouped: dict[str, list] = {}
    for facet in payload.get("facets") or []:
        raw_name = facet.get("rawName")
        if raw_name in facet_fields:
            grouped.setdefault(raw_name, []).extend(facet.get("values") or [])
    return grouped


# --------------------------------------------------------------------------------------
# Upsert dos catálogos
# --------------------------------------------------------------------------------------

def _sync_catalog(model, values: list, keep_codes: set[str] | None = None) -> dict:
    """Upsert de um catálogo (ProgrammeType/CallType) a partir dos values de uma faceta.

    `keep_codes` restringe aos códigos interessantes (usado no tipo, onde a faceta traz
    ORGANISATION, PERSON e outros que não são tipos de call).
    """
    created = updated = 0
    for entry in values:
        code = str(entry.get("rawValue") or "").strip()
        if not code or (keep_codes is not None and code not in keep_codes):
            continue
        name = str(entry.get("value") or code).strip()
        count = entry.get("count") or 0
        obj, was_created = model.objects.get_or_create(
            code=code, defaults={"name": name, "total_count": count},
        )
        if was_created:
            created += 1
        elif obj.name != name or obj.total_count != count:
            obj.name, obj.total_count = name, count
            obj.save(update_fields=["name", "total_count", "updated_at"])
            updated += 1
    return {"created": created, "updated": updated}


def _clean_label(value: str) -> str:
    """Nome legível a partir do `value` de uma faceta.

    Alguns nomes vêm URL-encoded (`Marie Sk%C5%82odowska-Curie Actions`), por isso passam por
    unquote. Só se faz isso quando há um '%' seguido de dois hex — caso contrário um nome com
    um '%' legítimo ("100% financiado") era corrompido.
    """
    text = str(value or "").strip()
    return unquote(text) if _PERCENT_ESCAPE.search(text) else text


def _sync_code_labels(facets: dict[str, list]) -> dict:
    """Upsert do catálogo genérico de códigos (CodeLabel) a partir das facetas."""
    created = updated = 0
    for facet_name in CODE_FACETS:
        for entry in facets.get(facet_name, []):
            code = str(entry.get("rawValue") or "").strip()
            if not code:
                continue
            label = _clean_label(entry.get("value")) or code
            obj, was_created = CodeLabel.objects.get_or_create(
                facet=facet_name, code=code, defaults={"label": label},
            )
            if was_created:
                created += 1
            elif obj.label != label:
                obj.label = label
                obj.save(update_fields=["label", "updated_at"])
                updated += 1
    return {"created": created, "updated": updated}


def sync_catalogs() -> dict:
    """Sincroniza ProgrammeType, CallType e CodeLabel a partir do /facet."""
    facets = _fetch_facets(["frameworkProgramme", "type", *CODE_FACETS])
    programmes = _sync_catalog(ProgrammeType, facets.get("frameworkProgramme", []))
    # Só os tipos que importamos — a faceta `type` traz também ORGANISATION, PERSON, etc.
    types = _sync_catalog(CallType, facets.get("type", []), keep_codes=set(IMPORTED_TYPES))
    codes = _sync_code_labels(facets)
    logger.info(
        "Catálogos de calls sincronizados: programas=%s tipos=%s códigos=%s",
        programmes, types, codes,
    )
    return {"programmes": programmes, "types": types, "codes": codes}


# --------------------------------------------------------------------------------------
# Upsert das calls
# --------------------------------------------------------------------------------------

def _documents(metadata: dict) -> list[dict]:
    """Documentos oficiais da call (guias, anexos), prontos a listar no front-end.

    Vêm em `publicationDocuments`, outra STRING com JSON. Só entram os que têm URL — sem
    link o registo não serve de nada a quem o quer descarregar.
    """
    entries = _json_field(_first(metadata.get("publicationDocuments")))
    if not isinstance(entries, list):
        return []
    documents = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("docUrl") or "").strip()
        if not url:
            continue
        documents.append({
            "name": str(entry.get("nameDoc") or "").strip(),
            "type": str(entry.get("typeDoc") or "").strip(),
            "language": str(entry.get("languageDoc") or "").strip(),
            "published_at": str(entry.get("finalPublicDocDate") or "").strip(),
            "url": url,
        })
    return documents


def _labels(metadata: dict, field: str, facet: str, catalog: dict) -> list[str]:
    """Códigos de `field` traduzidos pelos nomes da faceta `facet`.

    Só entram os que têm nome REAL. Para uma parte dos códigos a própria API não publica
    tradução — a faceta devolve o número como se fosse o nome (73 dos 172 destinos, 412 das
    755 divisões de programa) —, e mostrar "48080651" ao utilizador é pior do que não mostrar
    nada. Os códigos originais continuam todos no `raw`, para quem os precise.
    """
    names = []
    for code in _as_list(metadata.get(field)):
        code = str(code).strip()
        if not code:
            continue
        label = catalog.get((facet, code))
        if label and label != code:
            names.append(label)
    return names


def _call_url(call_type_code: str, project_acronym: str, status: str, api_url: str) -> str:
    """URL pública da call.

    Nas cascade funding calls (tipo 8), o `url` que a API devolve varia de formato conforme
    a call (`.../competitive-calls-cs/<ccm2Id>`, ou o antigo `.../data/topicDetails/<ref>`)
    e nem sempre abre a página certa no portal. Constrói-se antes a partir do
    `projectAcronym` — presente em todas as cascade calls verificadas —, que é o NOME do
    projeto que redistribui o financiamento (ex: "PHORTIFY", "DigiWind"), a palavra-chave
    que a pesquisa do portal usa para encontrar as suas calls, ao contrário do `identifier`
    técnico. `status` (código Open/Forthcoming) vai também na query, para a página abrir já
    filtrada pelo mesmo estado com que a call foi importada.
    Sem `project_acronym` (não deveria acontecer no tipo 8, mas por segurança), cai para o
    `url` da API em vez de gravar uma string vazia.
    """
    if call_type_code == "8" and project_acronym:
        # quote(): os acrónimos vêm com espaços e pontuação ("Cultural Horizons",
        # "BIRDS in BG - 2") — sem codificar, a query string fica tecnicamente inválida.
        return (
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/"
            "opportunities/calls-for-proposals?order=DESC&sortBy=startDate"
            f"&keywords={quote(project_acronym)}&status={status}"
        )
    return api_url


def _build_fields(result: dict, programmes: dict, types: dict, labels: dict) -> dict:
    """Campos do modelo `Call` a partir de um resultado CRU do /search.

    `result` é o objeto tal como a API o devolve — `{apiVersion, reference, url, summary,
    content, checksum, metadata: {...}, ...}` —, não só o bloco `metadata`. `raw` guarda o
    `result` INTEIRO, byte a byte: nada de desempacotar listas de 1 elemento nem de escolher
    só `metadata`. É a cópia verbatim que serve de fonte única quando o front-end precisar de
    algo sem campo próprio.

    `metadata` (a variável local) é uma VISTA efetiva: o bloco `metadata` do resultado com
    os campos do nível de topo por baixo. 5 das 1339 calls reais (tipo 8, "COMPETITIVE_CALL")
    não trazem a chave `metadata` de todo — os mesmos campos vêm soltos no nível de topo do
    resultado — e sem esta vista o resto da função (que lê `metadata.get(...)` diretamente)
    ficava cega para essas 5 calls.
    """
    metadata = {**{k: v for k, v in result.items() if k != "metadata"},
                **(result.get("metadata") or {})}
    deadlines = _as_list(metadata.get("deadlineDate"))
    identifier = str(_first(metadata.get("identifier")))[:255]
    call_type_code = str(_first(metadata.get("type")))
    # Calculados aqui e reaproveitados abaixo (colunas project_id/project_acronym), em vez de
    # reler `metadata` outra vez.
    project_id = str(_first(metadata.get("projectId")))[:50]
    project_acronym = str(_first(metadata.get("projectAcronym")))[:255]
    url = _call_url(
        call_type_code, project_acronym, str(_first(metadata.get("status"))),
        str(_first(metadata.get("url"))),
    )
    return {
        "identifier": identifier,
        "title": str(_first(metadata.get("title"))),
        "status": str(_first(metadata.get("status"))),
        "call_type": types.get(str(_first(metadata.get("type")))),
        "programme": programmes.get(str(_first(metadata.get("frameworkProgramme")))),
        "call_identifier": str(_first(metadata.get("callIdentifier")))[:255],
        "call_title": str(_first(metadata.get("callTitle"))),
        "start_date": _parse_datetime(_first(metadata.get("startDate"))),
        "deadline_date": _next_deadline(deadlines),
        "deadlines": deadlines,
        "deadline_model": str(_first(metadata.get("deadlineModel")))[:100],
        "programme_period": str(_first(metadata.get("programmePeriod")))[:100],
        "url": url,
        # Os Grants põem a descrição em `descriptionByte`; as cascade funding calls (tipo 8)
        # usam `description`. Sem o fallback, a coluna ficava vazia nas 113 cascade calls
        # que têm texto.
        "description": (
            str(_first(metadata.get("descriptionByte")))
            or str(_first(metadata.get("description")))
        ),
        "keywords": _as_list(metadata.get("keywords")),
        "types_of_action": str(_first(metadata.get("typesOfAction"))),
        "cross_cutting_priorities": _as_list(metadata.get("crossCuttingPriorities")),
        # Dimensões traduzidas de código para nome.
        "destination": "; ".join(_labels(metadata, "destination", "destination", labels)),
        "destination_group": "; ".join(
            _labels(metadata, "destinationGroup", "destinationGroup", labels)
        ),
        "mission": "; ".join(_labels(metadata, "mission", "mission", labels)),
        "geographical_zones": _labels(
            metadata, "geographicalZones", "geographicalZones", labels
        ),
        "contract_type": "; ".join(_labels(metadata, "contractType", "contractType", labels)),
        "specific_objective": "; ".join(
            _labels(metadata, "specificObjective", "specificObjective", labels)
        ),
        "programme_divisions": _labels(
            metadata, "programmeDivision", "programmeDivision", labels
        ),
        "tags": _as_list(metadata.get("tags")),
        "documents": _documents(metadata),
        # Cascade funding: o projeto que redistribui o financiamento.
        "project_name": str(_first(metadata.get("projectName"))),
        "project_acronym": project_acronym,
        "project_id": project_id,
        "duration": str(_first(metadata.get("duration"))),
        "ca_name": str(_first(metadata.get("caName"))),
        "allow_partner_search": _boolean(_first(metadata.get("allowPartnerSearch"))),
        "source_updated_at": _parse_datetime(_first(metadata.get("updateDate"))),
        # O resultado INTEIRO, tal como veio da API — ver docstring.
        "raw": result,
        **_budget(metadata, identifier),
        **_raw_fields(metadata),
    }


def _raw_fields(metadata: dict) -> dict:
    """Os campos CRUS da API, um a um, para as colunas que os guardam tal como vieram.

    `metadata` já é a vista mesclada (topo + metadata) construída em `_build_fields` — o
    fallback das 5 calls sem `metadata` já está resolvido antes de chegar aqui.
    """
    def s(key, limit=None):
        """String, já desempacotada de [valor]."""
        value = str(_first(metadata.get(key)))
        return value[:limit] if limit else value

    def dt(key):
        return _parse_datetime(_first(metadata.get(key)))

    def lst(key):
        return _as_list(metadata.get(key))

    return {
        # --- Envelope do motor de busca ---------------------------------------------
        "api_version": s("apiVersion", 20),
        "checksum": s("checksum", 128),
        "content": s("content"),
        "content_type": s("contentType", 50),
        "database": s("database", 50),
        "database_label": s("databaseLabel", 50),
        "group_by_id": s("groupById", 20),
        "summary": s("summary"),
        "citation": s("citation"),
        # weight/accessRestriction/enrichedMetadata são valores DIRETOS no envelope (não
        # embrulhados em lista, ao contrário de quase tudo o resto) — daí não passarem por
        # s()/lst(), que pressupõem [valor].
        "weight": (lambda v: float(v) if isinstance(v, (int, float)) else None)(
            metadata.get("weight")
        ),
        "access_restriction": (
            metadata.get("accessRestriction")
            if isinstance(metadata.get("accessRestriction"), bool) else None
        ),
        "children": lst("children"),
        "highlighted_fragments": lst("highlightedFragments"),
        "enriched_metadata": (
            metadata.get("enrichedMetadata")
            if isinstance(metadata.get("enrichedMetadata"), dict) else {}
        ),
        # --- Rastreio interno do motor de busca -------------------------------------
        # DATASOURCE/datasource nunca coexistem com valores diferentes (verificado); o
        # primeiro que tiver valor vence.
        "es_datasource": s("DATASOURCE") or s("datasource"),
        "es_combine": s("es_Combine", 10),
        "es_content_type": s("es_ContentType", 50),
        "es_sort_date": dt("es_SortDate"),
        "es_checksum": s("esST_checksum", 128),
        "es_filename": s("esST_FileName", 50),
        "es_url": s("esST_URL"),
        "es_first_ingest_date": dt("esDA_FirstIngestDate"),
        "es_ingest_date": dt("esDA_IngestDate"),
        "es_queue_date": dt("esDA_QueueDate"),
        "corporate_search_version": s("corporate-search-version", 20),
        "sort_status": s("sortStatus", 10),
        # --- Estruturas ainda em JSON string na API (guardadas cruas; a versão já
        #     descodificada vai no detalhe — ver serialize_call_detail) ----------------
        "actions": s("actions"),
        "budget_overview": s("budgetOverview"),
        "links_raw": s("links"),
        "latest_infos_raw": s("latestInfos"),
        # --- Textos crus adicionais --------------------------------------------------
        "description_byte": s("descriptionByte"),
        "topic_conditions_raw": s("topicConditions"),
        "destination_details_raw": s("destinationDetails"),
        "destination_description_raw": s("destinationDescription"),
        "beneficiary_administration_raw": s("beneficiaryAdministration"),
        "further_information_raw": s("furtherInformation"),
        "sep_template": s("sepTemplate"),
        "support_info_raw": s("supportInfo"),
        "language": s("language", 10),
        "focus_area": lst("focusArea"),
        "currency": s("currency", 10),
        "closing_date_raw": s("closingDate", 40),
        "update_date_raw": s("updateDate", 40),
        # --- Códigos crus (a versão traduzida em nome já vai nas colunas semânticas) ---
        "destination_code": s("destination", 50),
        "destination_group_code": s("destinationGroup", 50),
        "mission_code": s("mission", 50),
        "mission_group_code": s("missionGroup", 50),
        "geographical_zone_code": s("geographicalZone", 50),
        "contract_type_code": s("contractType", 50),
        "specific_objective_code": s("specificObjective", 50),
        "programme_division_codes": lst("programmeDivision"),
        "programme_division_prospect_code": s("programmeDivisionProspect", 50),
        "type_of_mgas": lst("typeOfMGAs"),
        "cft_id": s("cftId", 50),
        # --- Vocabulário interno do catálogo (CCM) ------------------------------------
        "ccm_tags": lst("ccmTags"),
        "ccm_tags2": lst("ccmTags2"),
        "es_in_ccm_tags": lst("esIN_ccmTags"),
        "es_in_ccm_tags2": lst("esIN_ccmTags2"),
        "cen_tags_a": lst("cenTagsA"),
    }


def sync_calls() -> dict:
    """Importa as calls (Grant, Calls for proposals, Cascade funding) para a base de dados.

    Percorre a paginação até uma página vazia, e para cada call faz upsert por `reference`:
    inexistente → cria; existente → atualiza SÓ se algum campo mudou (uma Forthcoming que
    abriu passa a Open, um prazo adiado é corrigido); igual → não toca.

    Devolve {"pages", "fetched", "created", "updated", "unchanged", "programmes", "types"}.
    """
    catalogs = sync_catalogs()
    # Catálogos em memória: sem isto era uma query por call só para resolver as duas FK.
    programmes = {p.code: p for p in ProgrammeType.objects.all()}
    types = {t.code: t for t in CallType.objects.all()}
    labels = {
        (c.facet, c.code): c.label
        for c in CodeLabel.objects.all().only("facet", "code", "label")
    }

    created = updated = unchanged = fetched = 0
    # A API repete a mesma `reference` em páginas diferentes; sem este registo, a repetição
    # contava como "updated" e o resumo do sync dava números enganadores.
    seen_references: set[str] = set()
    now = django_timezone.now()
    page_number = 1

    while page_number <= MAX_PAGES:
        results = _fetch_page(page_number)
        if not results:
            break
        for result in results:
            reference = str(result.get("reference") or "").strip()
            # NÃO exigir `metadata`: 5 das 1339 calls reais (tipo 8, "COMPETITIVE_CALL") não
            # trazem essa chave — os mesmos campos vêm soltos no nível de topo do resultado
            # (ver `_field`). Exigir `metadata` aqui descartava essas 5 calls em silêncio,
            # apesar de terem `reference` e `identifier` válidos. `identifier` é o teste de
            # "há dados de facto", lido com o mesmo fallback usado no resto do sync.
            identifier = _first(_field(result, "identifier"))
            if not reference or not identifier:
                continue
            fetched += 1
            if reference in seen_references:
                continue
            seen_references.add(reference)

            fields = _build_fields(result, programmes, types, labels)
            with transaction.atomic():
                existing = Call.objects.filter(reference=reference).first()
                if existing is None:
                    Call.objects.create(
                        reference=reference, last_seen_at=now,
                        last_update_source=Call.SOURCE_API, **fields,
                    )
                    created += 1
                    continue
                changed = [
                    name for name, value in fields.items()
                    if getattr(existing, name) != value
                ]
                if changed:
                    for name in changed:
                        setattr(existing, name, fields[name])
                    existing.last_seen_at = now
                    # A API sobrescreve o que a call tiver de edição manual anterior — mesmo
                    # comportamento de avisos.Grant/db_service: o sync é sempre a origem mais
                    # recente quando corre, `last_update_source` só regista QUEM tocou por
                    # último, não impede a escrita.
                    existing.last_update_source = Call.SOURCE_API
                    existing.last_updated_by = None
                    existing.save(update_fields=[
                        *changed, "last_seen_at", "updated_at",
                        "last_update_source", "last_updated_by",
                    ])
                    updated += 1
                else:
                    existing.last_seen_at = now
                    existing.save(update_fields=["last_seen_at"])
                    unchanged += 1
        page_number += 1
    else:
        logger.warning(
            "Sync de calls parou no limite de %s páginas — a API continuava a devolver "
            "resultados. Pode haver calls por importar.", MAX_PAGES,
        )

    summary = {
        "pages": page_number - 1,
        "fetched": fetched,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "programmes": catalogs["programmes"],
        "types": catalogs["types"],
        "codes": catalogs["codes"],
    }
    logger.info("Sync de calls concluído: %s", summary)
    return summary


# --------------------------------------------------------------------------------------
# Serialização e listagem
# --------------------------------------------------------------------------------------

# Ordenações suportadas pela listagem (?order_by=). Default (Meta.ordering): início mais recente.
# As calls sem data ficam sempre no FIM (`nulls_last`): 5 calls não trazem prazo e, com a
# ordenação simples do Postgres, apareciam à frente de todas as outras no "prazo mais próximo"
# — o topo da listagem seria ocupado por calls sem prazo nenhum.
ORDERING = {
    "start_latest": F("start_date").desc(nulls_last=True),
    "start_earliest": F("start_date").asc(nulls_last=True),
    "deadline_earliest": F("deadline_date").asc(nulls_last=True),
    "deadline_latest": F("deadline_date").desc(nulls_last=True),
    "budget_highest": F("budget_total").desc(nulls_last=True),
    "budget_lowest": F("budget_total").asc(nulls_last=True),
}


def _amount(value):
    """Decimal -> float para JSON (o json do Django não serializa Decimal)."""
    return float(value) if value is not None else None


def _distinct_values(field: str) -> list[str]:
    """Valores distintos de `field`, ordenados, para os selects do front-end.

    O `order_by()` vazio é obrigatório: o `Meta.ordering` do modelo (-start_date, identifier)
    entra no SELECT DISTINCT e torna cada linha única, devolvendo as 1140 calls em vez dos
    71 valores distintos.
    """
    return sorted(
        Call.objects.exclude(**{field: ""})
        .order_by()
        .values_list(field, flat=True)
        .distinct()
    )


def distinct_action_types() -> list[str]:
    """Tipos de ação presentes nas calls, para o select do front-end."""
    return _distinct_values("types_of_action")


def distinct_missions() -> list[str]:
    """Missões da UE presentes nas calls (Oceanos, Solo, Cidades...)."""
    return _distinct_values("mission")


def distinct_destinations() -> list[str]:
    """Destinos/áreas temáticas presentes nas calls."""
    return _distinct_values("destination")


def top_tags(limit: int = 100) -> list[str]:
    """Tags temáticas mais frequentes.

    São 2276 distintas: mandá-las todas para um select não ajudava ninguém, por isso vão as
    `limit` mais usadas (o resto continua a chegar-se pela pesquisa livre).
    """
    counts: dict[str, int] = {}
    for tags in Call.objects.exclude(tags=[]).values_list("tags", flat=True):
        for tag in tags or []:
            counts[tag] = counts.get(tag, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [name for name, _ in ranked[:limit]]


def distinct_priorities() -> list[str]:
    """Prioridades transversais presentes nas calls, ordenadas pelas mais frequentes.

    É uma lista dentro de um JSONField, por isso a contagem é feita em Python — são ~1300
    linhas com listas curtas, e o custo não justifica o SQL específico do Postgres.
    """
    counts: dict[str, int] = {}
    for priorities in Call.objects.exclude(cross_cutting_priorities=[]).values_list(
        "cross_cutting_priorities", flat=True
    ):
        for priority in priorities or []:
            counts[priority] = counts.get(priority, 0) + 1
    return [name for name, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def serialize_call(call: Call) -> dict:
    """Uma call no formato devolvido pela API (sem o `raw`, que é volumoso)."""
    return {
        "id": call.id,
        "reference": call.reference,
        "identifier": call.identifier,
        "title": call.title,
        "status": call.status,
        "status_label": call.get_status_display(),
        "type": call.call_type.name if call.call_type else None,
        "type_code": call.call_type.code if call.call_type else None,
        "programme": call.programme.name if call.programme else None,
        "programme_code": call.programme.code if call.programme else None,
        "call_identifier": call.call_identifier,
        "call_title": call.call_title,
        "start_date": call.start_date.isoformat() if call.start_date else None,
        "deadline_date": call.deadline_date.isoformat() if call.deadline_date else None,
        "deadlines": call.deadlines,
        "deadline_model": call.deadline_model,
        "programme_period": call.programme_period,
        "url": call.url,
        "keywords": call.keywords,
        "types_of_action": call.types_of_action,
        "cross_cutting_priorities": call.cross_cutting_priorities,
        "budget_total": _amount(call.budget_total),
        "min_contribution": _amount(call.min_contribution),
        "max_contribution": _amount(call.max_contribution),
        "expected_grants": call.expected_grants,
        # Diz de onde veio o montante: "detailed" traz gama e nº de grants (Grants), "simple"
        # é só o total (cascade/proposals) e pode ter a escala ambígua — ver Call.budget_total.
        "budget_source": (
            None if call.budget_total is None
            else "detailed" if call.min_contribution is not None else "simple"
        ),
        # Dimensões temáticas/geográficas, já em nome legível.
        "destination": call.destination,
        "destination_group": call.destination_group,
        "mission": call.mission,
        "programme_divisions": call.programme_divisions,
        "geographical_zones": call.geographical_zones,
        "contract_type": call.contract_type,
        "specific_objective": call.specific_objective,
        "tags": call.tags,
        # Contagem em vez da lista: a listagem fica leve e o front-end sabe se vale a pena
        # abrir o detalhe para ir buscar os ficheiros.
        "documents_count": len(call.documents or []),
        "ca_name": call.ca_name,
        "project_name": call.project_name,
        "project_acronym": call.project_acronym,
        "duration": call.duration,
        "allow_partner_search": call.allow_partner_search,
        "source_updated_at": (
            call.source_updated_at.isoformat() if call.source_updated_at else None
        ),
        # Origem da última escrita — 'api' (sync) ou 'manual' (PUT/PATCH em /edit/). Um sync
        # SEGUINTE volta a marcar 'api' se tocar em algum campo (ver services.sync_calls).
        "last_update_source": call.last_update_source,
        "last_updated_by": call.last_updated_by.username if call.last_updated_by else None,
        "updated_at": call.updated_at.isoformat() if call.updated_at else None,
    }


def serialize_call_detail(call: Call) -> dict:
    """Detalhe: tudo o que a listagem dá, mais os textos longos e TODOS os campos crus.

    Lê das colunas próprias (não do `raw`): desde que `Call` passou a ter uma coluna por
    campo da API (ver `_raw_fields`), reparsear o `raw` aqui era trabalho a dobrar. As
    estruturas que a API manda como string-com-JSON (`budgetOverview`, `actions`, `links`,
    `latestInfos`) vão descodificadas, para o front-end não ter de fazer JSON.parse dentro
    de JSON.
    """
    return {
        **serialize_call(call),
        # Textos ricos (HTML) — só existem em parte das calls.
        "description": call.description,
        "topic_conditions": call.topic_conditions_raw,
        "support_info": call.support_info_raw,
        "destination_details": call.destination_details_raw,
        "destination_description": call.destination_description_raw,
        "beneficiary_administration": call.beneficiary_administration_raw,
        "submission_info": call.sep_template,
        # Cascade funding (tipo 8): prazo de fecho e links extra do projeto-mãe.
        "project_id": call.project_id,
        "closing_date": call.closing_date_raw,
        "further_information": [call.further_information_raw] if call.further_information_raw else [],
        "latest_infos": _json_field(call.latest_infos_raw) or [],
        "links": _json_field(call.links_raw) or [],
        # Documentos oficiais completos (na listagem vai só a contagem).
        "documents": call.documents,
        # Estruturas já descodificadas (a coluna guarda a string crua; aqui vai o objeto).
        "budget_overview": _json_field(call.budget_overview),
        "actions": _json_field(call.actions),
        # --- Todos os campos crus da API, um a um, tal como vieram --------------------
        "raw_fields": {
            "api_version": call.api_version,
            "checksum": call.checksum,
            "content": call.content,
            "content_type": call.content_type,
            "database": call.database,
            "database_label": call.database_label,
            "group_by_id": call.group_by_id,
            "summary": call.summary,
            "citation": call.citation,
            "weight": call.weight,
            "access_restriction": call.access_restriction,
            "children": call.children,
            "highlighted_fragments": call.highlighted_fragments,
            "enriched_metadata": call.enriched_metadata,
            "es_datasource": call.es_datasource,
            "es_combine": call.es_combine,
            "es_content_type": call.es_content_type,
            "es_sort_date": call.es_sort_date.isoformat() if call.es_sort_date else None,
            "es_checksum": call.es_checksum,
            "es_filename": call.es_filename,
            "es_url": call.es_url,
            "es_first_ingest_date": (
                call.es_first_ingest_date.isoformat() if call.es_first_ingest_date else None
            ),
            "es_ingest_date": call.es_ingest_date.isoformat() if call.es_ingest_date else None,
            "es_queue_date": call.es_queue_date.isoformat() if call.es_queue_date else None,
            "corporate_search_version": call.corporate_search_version,
            "sort_status": call.sort_status,
            "description_byte": call.description_byte,
            "topic_conditions_raw": call.topic_conditions_raw,
            "language": call.language,
            "focus_area": call.focus_area,
            "currency": call.currency,
            "destination_code": call.destination_code,
            "destination_group_code": call.destination_group_code,
            "mission_code": call.mission_code,
            "mission_group_code": call.mission_group_code,
            "geographical_zone_code": call.geographical_zone_code,
            "contract_type_code": call.contract_type_code,
            "specific_objective_code": call.specific_objective_code,
            "programme_division_codes": call.programme_division_codes,
            "programme_division_prospect_code": call.programme_division_prospect_code,
            "type_of_mgas": call.type_of_mgas,
            "cft_id": call.cft_id,
            "ccm_tags": call.ccm_tags,
            "ccm_tags2": call.ccm_tags2,
            "es_in_ccm_tags": call.es_in_ccm_tags,
            "es_in_ccm_tags2": call.es_in_ccm_tags2,
            "cen_tags_a": call.cen_tags_a,
        },
        # O resultado COMPLETO e cru da API — envelope + metadata, tudo em listas, sem
        # qualquer transformação. Fica como rede de segurança para o que não tiver coluna.
        "raw": call.raw,
    }


def list_calls(*, status=None, programme_code=None, type_code=None, order_by=None,
               search=None, priority=None, action_type=None, has_budget=None,
               tag=None, mission=None, destination=None, has_documents=None):
    """Queryset de calls filtrado e ordenado. Um argumento a None = "não filtrar por isso"."""
    queryset = Call.objects.select_related("call_type", "programme")
    if status:
        queryset = queryset.filter(status=status)
    if programme_code:
        queryset = queryset.filter(programme__code=programme_code)
    if type_code:
        queryset = queryset.filter(call_type__code=type_code)
    if search:
        # O identificador é o que os comerciais têm à mão (vem nos documentos da call), por
        # isso a pesquisa cobre-o a par do título.
        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(identifier__icontains=search)
            | Q(call_title__icontains=search)
            | Q(ca_name__icontains=search)
            | Q(project_name__icontains=search)
        )
    if priority:
        queryset = queryset.filter(cross_cutting_priorities__contains=[priority])
    if action_type:
        queryset = queryset.filter(types_of_action__icontains=action_type)
    if has_budget is not None:
        queryset = queryset.filter(budget_total__isnull=not has_budget)
    if tag:
        queryset = queryset.filter(tags__contains=[tag])
    if mission:
        queryset = queryset.filter(mission__icontains=mission)
    if destination:
        queryset = queryset.filter(destination__icontains=destination)
    if has_documents is not None:
        # Uma lista JSON vazia não é NULL, por isso o teste é sobre o conteúdo.
        queryset = (
            queryset.exclude(documents=[]) if has_documents else queryset.filter(documents=[])
        )
    ordering = ORDERING.get(order_by or "")
    return queryset.order_by(ordering) if ordering else queryset
