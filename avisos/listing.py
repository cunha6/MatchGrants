"""Filtragem, ordenação e resumo da listagem de avisos.

Separado das views porque é lógica de NEGÓCIO, não de HTTP: decide que avisos casam com os
critérios pedidos (reutilizando as MESMAS regras de elegibilidade do motor de match) e por
que ordem aparecem. A view fica só com o que lhe compete — papéis, paginação e resposta.

Recebe `params` (o QueryDict de request.GET) em vez do request: é a única coisa de que
precisa, e assim isto é testável sem construir um pedido HTTP.

Porquê filtrar em Python e não tudo em SQL: as datas dos avisos vêm em TEXTO LIVRE (o scrape
não as consegue normalizar sempre) e a taxa de financiamento é CALCULADA das linhas-filhas
(ver serializers.financing_rate). O que dá para filtrar em SQL — pesquisa, estado, dotação —
é filtrado em SQL; o resto tem de ser em Python.
"""

from datetime import date

from django.db.models import Q

from common.dates import parse_date
from common.text import normalize as _normalize
from match.scoring_rules import eligible_cae, grant_allowed_dimensions
from .models import Grant
from .serializers import financing_rate, grant_summary, nearest_phase_date


def _float_param(params, name):
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace(",", "."))
    except ValueError:
        return None


def _in_date_range(text, date_from, date_to) -> bool:
    """Um valor de data em texto livre está dentro do intervalo pedido? Sem data legível, e
    havendo filtro de data, exclui (não dá para provar que cabe)."""
    if date_from is None and date_to is None:
        return True
    parsed_value = parse_date(text)
    if parsed_value is None:
        return False
    if date_from and parsed_value < date_from:
        return False
    if date_to and parsed_value > date_to:
        return False
    return True


def _cae_matches(grant: Grant, cae: str) -> bool:
    """O CAE pesquisado é elegível no aviso, com a REGRA DO PREFIXO (reutiliza `match.eligible_cae`):
    um aviso com incluído '55***' casa com '55849'; um com incluído '55847' NÃO casa com '55848'.
    Aviso sem restrição de CAE casa com qualquer CAE."""
    return eligible_cae(
        {"cae_codes": [cae]},
        {"included_caes": grant.included_caes or [], "excluded_caes": grant.excluded_caes or []},
    )


def _dimensions_match(grant: Grant, dims: set[str]) -> bool:
    """O aviso admite alguma das dimensões pedidas. Reutiliza `grant_allowed_dimensions` sobre o
    mesmo texto de elegibilidade do match (critérios de beneficiário + destinatários finais).
    Aviso sem restrição de dimensão admite todas."""
    text = _normalize(" | ".join(str(criterion_text) for criterion_text in (
        list(grant.beneficiary_eligibility_criteria or []) + list(grant.final_recipients or [])
    )))
    allowed = grant_allowed_dimensions(text)
    return not allowed or bool(dims & allowed)


def _region_matches(grant: Grant, region: str) -> bool:
    """A região pesquisada casa com as regiões elegíveis OU as áreas abrangidas do aviso
    (contains bidirecional sobre texto normalizado). Aviso sem regiões nem áreas (âmbito não
    restrito) casa com qualquer pesquisa."""
    haystack = [_normalize(region_text) for region_text in (grant.eligible_regions or []) if region_text]
    haystack += [_normalize(covered_area.geographic_area) for covered_area in grant.covered_areas.all() if covered_area.geographic_area]
    haystack = [h for h in haystack if h]
    if not haystack:
        return True
    return any(region in h or h in region for h in haystack)


def _dimension_params(params) -> set[str]:
    """Dimensões pedidas em `?dimension=` — aceita repetido (?dimension=micro&dimension=media) e
    separado por vírgulas (?dimension=micro,media). Normalizadas (micro/pequena/media/grande)."""
    raw = []
    for value in params.getlist("dimension"):
        raw += value.split(",")
    return {_normalize(parsed_value) for parsed_value in raw if parsed_value.strip()}


# Ordenações suportadas por ?order_by= (default: publication_recent). Cada uma dá (chave de
# ordenação, reverse) sobre um "row" já calculado (ver list_grants) — nunca sobre a página
# devolvida: a lista inteira que passa nos filtros é ordenada ANTES de paginar, por isso o
# aviso com a maior dotação (ou a data mais próxima, ou a maior taxa) aparece sempre em
# primeiro lugar mesmo que não estivesse na primeira página por ordem de inserção na BD.
_ORDER_BY = {
    "publication_recent":  (lambda region_text: region_text["pub"],        True),
    "publication_oldest":  (lambda region_text: region_text["pub"],        False),
    "closing_earliest":    (lambda region_text: region_text["closing"],     False),
    "closing_latest":      (lambda region_text: region_text["closing"],     True),
    "allocation_highest":  (lambda region_text: region_text["allocation"],  True),
    "allocation_lowest":   (lambda region_text: region_text["allocation"],  False),
    "rate_highest":        (lambda region_text: region_text["rate"],        True),
    "rate_lowest":         (lambda region_text: region_text["rate"],        False),
}


def _base_queryset(params):
    """Os filtros que DÃO para fazer em SQL: pesquisa (código/título), estado e dotação.

    A pesquisa filtra o queryset INTEIRO, não só a página devolvida — por isso vai aqui e não
    no ciclo em Python.
    """
    qs = Grant.objects.filter(ai_processed=True).prefetch_related(
        "phases", "phase_areas", "financing_rates", "covered_areas",
    )
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(grant_code__icontains=q) | Q(title__icontains=q))

    active = (params.get("active") or "true").lower()
    if active == "true":
        qs = qs.filter(active=True)
    elif active == "false":
        qs = qs.filter(active=False)

    allocation_min = _float_param(params, "allocation_min")
    allocation_max = _float_param(params, "allocation_max")
    if allocation_min is not None:
        qs = qs.filter(total_allocation__gte=allocation_min)
    if allocation_max is not None:
        qs = qs.filter(total_allocation__lte=allocation_max)
    return qs


def _passes_eligibility_filters(grant: Grant, filters: dict) -> bool:
    """Filtros que não dão para fazer em SQL: datas em texto livre e as regras de
    elegibilidade do match (CAE por prefixo, região/área, dimensão).

    Corridos ANTES do filtro por taxa, porque a taxa é o único critério que obriga a um
    cálculo sobre as linhas-filhas — assim só se calcula para quem já passou daqui.
    """
    if not _in_date_range(grant.publication_date, filters["pub_from"], filters["pub_to"]):
        return False
    if not _in_date_range(grant.closing_date, filters["close_from"], filters["close_to"]):
        return False
    if filters["cae"] and not _cae_matches(grant, filters["cae"]):
        return False
    if filters["region"] and not _region_matches(grant, filters["region"]):
        return False
    if filters["dimensions"] and not _dimensions_match(grant, filters["dimensions"]):
        return False
    return True


def _passes_rate_filters(rate: float | None, filters: dict) -> bool:
    """?rate_min/?rate_max sobre a taxa EFETIVA. Um aviso sem taxa declarada (None) não passa
    quando há filtro de taxa — não dá para provar que cabe no intervalo."""
    if filters["rate_min"] is not None and (rate is None or rate < filters["rate_min"]):
        return False
    if filters["rate_max"] is not None and (rate is None or rate > filters["rate_max"]):
        return False
    return True


def _sort(summary_rows: list[dict], order_by: str | None) -> list[dict]:
    """Ordena os resumos. Os avisos SEM valor no critério pedido vão sempre para o FIM, nunca
    para o início, seja qual for a direção — senão uma ordenação decrescente traria primeiro
    os avisos sem dotação/taxa declarada."""
    key_fn, reverse = _ORDER_BY.get(order_by, _ORDER_BY["publication_recent"])
    with_value = sorted((region_text for region_text in summary_rows if key_fn(region_text) is not None), key=key_fn, reverse=reverse)
    without_value = [region_text for region_text in summary_rows if key_fn(region_text) is None]
    return [region_text["summary"] for region_text in with_value + without_value]


def list_grants(params) -> list[dict]:
    """Resumos dos avisos que passam nos filtros de `params`, já ordenados.

    `params` é o QueryDict de request.GET. A ordenação é feita sobre TODOS os avisos que
    passam nos filtros — a paginação é aplicada DEPOIS, por quem chama.
    """
    python_filters = {
        "pub_from": parse_date(params.get("publication_from")),
        "pub_to": parse_date(params.get("publication_to")),
        "close_from": parse_date(params.get("closing_from")),
        "close_to": parse_date(params.get("closing_to")),
        "rate_min": _float_param(params, "rate_min"),
        "rate_max": _float_param(params, "rate_max"),
        "cae": (params.get("cae") or "").strip(),
        "region": _normalize(params.get("region")),
        "dimensions": _dimension_params(params),
    }
    today = date.today()

    summary_rows = []
    for grant in _base_queryset(params):
        if not _passes_eligibility_filters(grant, python_filters):
            continue
        rate = financing_rate(grant)
        if not _passes_rate_filters(rate, python_filters):
            continue
        summary_rows.append({
            "pub": parse_date(grant.publication_date),
            "closing": parse_date(grant.closing_date),
            "allocation": grant.total_allocation,
            "rate": rate,
            "summary": grant_summary(grant, rate, nearest_phase_date(grant, today)),
        })

    return _sort(summary_rows, params.get("order_by"))
