"""Endpoints da app avisos: listagem (exige sessão), detalhe (autenticados: qualquer aviso;
sem sessão: só os avisos que o match dessa sessão devolveu — ver common/session_access.py),
edição (admin+commercial_grants+commercial_public) e scrape (aberto, para automação diária).
As views são magras — a lógica vive em service/db."""

import json
import logging
import mimetypes
import os
from datetime import date

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, transaction
from django.db.models import Q
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from common.dates import parse_date
from common.files import safe_media_path
from common.pagination import paginate
from common.session_access import can_view_grant
from common.text import normalize as _normalize
from users.models import UserProfile
from users.permissions import require_role
from . import service
from match.ranking import _norm, max_financing_rate_from_rates
from match.scoring_rules import eligible_cae, grant_allowed_dimensions
from .models import FinancingRate, Grant, PhaseArea
from .notifications import notify_grants

# Pasta (relativa ao BASE_DIR) onde ficam os PDFs dos avisos descarregados.
_AVISOS_DIR = "pdf_Avisos"

logger = logging.getLogger(__name__)
# Trilho de auditoria das edições de avisos (quem alterou o quê) → consola + logs/avisos.log.
audit_logger = logging.getLogger("avisos.audit")


def _audit_value(value, limit: int = 300) -> str:
    """Representação curta de um valor para o log de auditoria (trunca listas/textos longos)."""
    s = repr(value)
    return s if len(s) <= limit else s[:limit] + "…[truncado]"

# Editar um aviso (endpoint próprio) — ADMIN + os dois comerciais (avisos é domínio de ambos:
# COMMERCIAL_GRANTS é especialista, COMMERCIAL_PUBLIC acumula avisos+anúncios).
_EDIT_ROLES = (UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)

# Campos NÃO editáveis (além da chave primária): são reescritos a cada processamento do
# aviso — `annex_documents` vem fresco do scrape da página e `applicable_legislation` da
# extração da IA. Editá-los à mão dava a ilusão de persistência: a alteração seria
# silenciosamente descartada no próximo scrape/extração. `last_update_source`/`last_updated_by`
# são geridos pelo próprio código (não pelo payload do cliente) — ver grants_edit.
_NON_EDITABLE_FIELDS = frozenset({
    "annex_documents", "applicable_legislation", "last_update_source", "last_updated_by",
})

# "Tudo menos o id" (e os campos regenerados acima): qualquer outro campo próprio do aviso
# pode ser editado.
_EDITABLE_FIELDS = frozenset(
    f.name for f in Grant._meta.fields
    if not f.primary_key and f.name not in _NON_EDITABLE_FIELDS
)

# Não expostos na resposta tal-qual (vetor de embedding/hash — ruído binário/interno;
# `last_updated_by` é uma FK — não serializável diretamente, tratada à parte em _grant_detail).
_DETAIL_SKIP = {"activity_embedding", "activity_embedding_hash", "last_updated_by"}


# --- Helpers de ranking/serialização ---------------------------------------

def _computed_financing_rate(grant: Grant) -> float | None:
    """Taxa representativa CALCULADA das linhas-filhas: a maior taxa de comparticipação das
    linhas de dotação (ignorando a 'Dotação Global' a 100%, que é fundo+contrapartida);
    em falta, a maior das FinancingRate. None se o aviso não declarar taxa."""
    pa_rates = [
        pa.max_financing_rate for pa in grant.phase_areas.all()
        if pa.max_financing_rate is not None and _norm(pa.fund_name) != "dotacao global"
    ]
    if pa_rates:
        return max(pa_rates)
    return max_financing_rate_from_rates([
        {"max_global_rate": fr.max_global_rate, "base_rate": fr.base_rate}
        for fr in grant.financing_rates.all()
    ])


def _financing_rate(grant: Grant) -> float | None:
    """Taxa de financiamento EFETIVA mostrada: o override manual (`grant.financing_rate`)
    quando definido; senão a taxa calculada das linhas-filhas. É este valor que aparece na
    listagem e no detalhe e que serve para ordenar/filtrar por taxa."""
    if grant.financing_rate is not None:
        return grant.financing_rate
    return _computed_financing_rate(grant)


def _nearest_phase_date(grant: Grant, today: date) -> date | None:
    """Data da fase MAIS PRÓXIMA: a data de fase mais cedo que ainda não passou; se todas já
    passaram, a mais recente. None se o aviso não tiver fases com datas legíveis."""
    dates = []
    for p in grant.phases.all():
        for raw in (p.start_date, p.end_date):
            d = parse_date(raw)
            if d:
                dates.append(d)
    if not dates:
        return None
    upcoming = [d for d in dates if d >= today]
    return min(upcoming) if upcoming else max(dates)


def _grant_summary(grant: Grant, rate: float | None, nearest: date | None) -> dict:
    """Resumo ENXUTO para a listagem (o front-end mostra só o essencial; o detalhe vem do GET
    /avisos/<id>/). Campos: código, título, data final, dotação, taxa de financiamento."""
    return {
        "id": grant.id,
        "grant_code": grant.grant_code,
        "title": grant.title,
        "closing_date": grant.closing_date,
        "total_allocation": grant.total_allocation,
        "financing_rate": rate,
        "next_phase_date": nearest.isoformat() if nearest else None,
        "active": grant.active,
    }


def _serialize_financing_rate(fr: FinancingRate) -> dict:
    """Uma linha de taxa de financiamento (usada no detalhe e na edição de coleção)."""
    return {
        "company_size": fr.company_size, "aid_regime": fr.aid_regime, "base_rate": fr.base_rate,
        "regional_bonus": fr.regional_bonus, "max_global_rate": fr.max_global_rate,
        "minimis_accumulation_limit": fr.minimis_accumulation_limit,
        "specific_condition": fr.specific_condition,
    }


def _serialize_phase_area(pa: PhaseArea) -> dict:
    """Uma linha de dotação/taxa por fase×área (usada no detalhe e na edição de coleção)."""
    return {
        "phase_id": pa.phase_id, "area_id": pa.area_id, "fund_name": pa.fund_name,
        "budget_allocation": pa.budget_allocation, "max_financing_rate": pa.max_financing_rate,
        "distribution": pa.distribution,
    }


def _grant_detail(grant: Grant) -> dict:
    """Serialização COMPLETA do aviso (todos os campos + relações) para o GET de detalhe."""
    data = {f.name: getattr(grant, f.name)
            for f in grant._meta.fields if f.name not in _DETAIL_SKIP}
    # Quem/o quê fez a última escrita: 'scrape' (pipeline) ou 'manual' (edição) + o utilizador,
    # quando aplicável (username, não o objeto User — que não é serializável em JSON).
    data["last_updated_by"] = grant.last_updated_by.username if grant.last_updated_by_id else None
    # `financing_rate` = valor EFETIVO (override manual ou calculado). `financing_rate_manual`
    # diz ao front-end se está fixado à mão (para o formulário mostrar "auto" vs valor fixo).
    data["financing_rate"] = _financing_rate(grant)
    data["financing_rate_manual"] = grant.financing_rate is not None
    data["phases"] = [
        {"id": p.id, "name": p.name, "start_date": p.start_date,
         "end_date": p.end_date, "access_condition": p.access_condition}
        for p in grant.phases.all()
    ]
    data["covered_areas"] = [
        {"id": a.id, "geographic_area": a.geographic_area} for a in grant.covered_areas.all()
    ]
    data["phase_areas"] = [_serialize_phase_area(pa) for pa in grant.phase_areas.all()]
    data["financing_rates"] = [_serialize_financing_rate(fr) for fr in grant.financing_rates.all()]
    data["expense_limits"] = [
        {"expense_category": el.expense_category,
         "applicable_ocs_methodology": el.applicable_ocs_methodology,
         "max_absolute_value": el.max_absolute_value,
         "max_percentage_value": el.max_percentage_value,
         "calculation_base": el.calculation_base, "specific_conditions": el.specific_conditions}
        for el in grant.expense_limits.all()
    ]
    data["non_compliance_penalties"] = [
        {"indicator_types": p.indicator_types, "compliance_grade_formula": p.compliance_grade_formula,
         "penalty_tiers": p.penalty_tiers, "max_penalty_percentage": p.max_penalty_percentage,
         "rule_description": p.rule_description}
        for p in grant.non_compliance_penalties.all()
    ]
    data["evaluation_methodologies"] = [
        {"project_merit_formula": m.project_merit_formula, "scoring_scale": m.scoring_scale,
         "min_global_score": m.min_global_score, "evaluation_criteria": m.evaluation_criteria,
         "tiebreaker_criteria": m.tiebreaker_criteria}
        for m in grant.evaluation_methodologies.all()
    ]
    data["beneficiaries_by_action"] = [
        {"action_type": b.action_type, "entities": b.entities}
        for b in grant.beneficiaries_by_action.all()
    ]
    data["documents"] = [
        {"doc_type": d.doc_type, "name": d.name, "url": d.url, "is_canonical": d.is_canonical}
        for d in grant.documents.all()
    ]
    # O PDF do próprio aviso (descarregado em pdf_Avisos): link para abrir no browser.
    # `document_url` só vem preenchido se o ficheiro existir mesmo em disco. Também entra
    # como PRIMEIRO item da lista de documentos (o front-end abre-o com target="_blank").
    doc_url = f"/avisos/{grant.id}/document/" if safe_media_path(grant.pdf_path, _AVISOS_DIR) else None
    data["document_url"] = doc_url
    if doc_url:
        data["documents"].insert(0, {
            "doc_type": "aviso", "name": os.path.basename(grant.pdf_path),
            "url": doc_url, "is_canonical": True, "local": True,
        })
    return data


def serve_grant_document(request, pk):
    """Serve o PDF do aviso (pdf_Avisos) para abrir no browser (inline). Público.
    O front-end liga a este URL com target=\"_blank\"."""
    grant = Grant.objects.filter(pk=pk).only("id", "pdf_path").first()
    if grant is None:
        return JsonResponse({"error": "Aviso não encontrado."}, status=404)
    path = safe_media_path(grant.pdf_path, _AVISOS_DIR)
    if not path:
        return JsonResponse({"error": "Documento do aviso não disponível."}, status=404)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(open(path, "rb"), content_type=ctype,
                        as_attachment=False, filename=os.path.basename(path))


# --- Filtros da listagem ----------------------------------------------------

def _float_param(request, name):
    raw = request.GET.get(name)
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
    d = parse_date(text)
    if d is None:
        return False
    if date_from and d < date_from:
        return False
    if date_to and d > date_to:
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
    text = _normalize(" | ".join(str(p) for p in (
        list(grant.beneficiary_eligibility_criteria or []) + list(grant.final_recipients or [])
    )))
    allowed = grant_allowed_dimensions(text)
    return not allowed or bool(dims & allowed)


def _region_matches(grant: Grant, region: str) -> bool:
    """A região pesquisada casa com as regiões elegíveis OU as áreas abrangidas do aviso
    (contains bidirecional sobre texto normalizado). Aviso sem regiões nem áreas (âmbito não
    restrito) casa com qualquer pesquisa."""
    haystack = [_normalize(r) for r in (grant.eligible_regions or []) if r]
    haystack += [_normalize(a.geographic_area) for a in grant.covered_areas.all() if a.geographic_area]
    haystack = [h for h in haystack if h]
    if not haystack:
        return True
    return any(region in h or h in region for h in haystack)


def _dimension_params(request) -> set[str]:
    """Dimensões pedidas em `?dimension=` — aceita repetido (?dimension=micro&dimension=media) e
    separado por vírgulas (?dimension=micro,media). Normalizadas (micro/pequena/media/grande)."""
    raw = []
    for value in request.GET.getlist("dimension"):
        raw += value.split(",")
    return {_normalize(d) for d in raw if d.strip()}


# Ordenações suportadas por ?order_by= (default: publication_recent). Cada uma dá (chave de
# ordenação, reverse) sobre um "row" já calculado (ver grants_list) — nunca sobre a página
# devolvida: a lista inteira que passa nos filtros é ordenada ANTES de paginar, por isso o
# aviso com a maior dotação (ou a data mais próxima, ou a maior taxa) aparece sempre em
# primeiro lugar mesmo que não estivesse na primeira página por ordem de inserção na BD.
_ORDER_BY = {
    "publication_recent":  (lambda r: r["pub"],        True),
    "publication_oldest":  (lambda r: r["pub"],        False),
    "closing_earliest":    (lambda r: r["closing"],     False),
    "closing_latest":      (lambda r: r["closing"],     True),
    "allocation_highest":  (lambda r: r["allocation"],  True),
    "allocation_lowest":   (lambda r: r["allocation"],  False),
    "rate_highest":        (lambda r: r["rate"],        True),
    "rate_lowest":         (lambda r: r["rate"],        False),
}


@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC,
              UserProfile.CLIENT)
def grants_list(request):
    """Listagem ENXUTA dos avisos — exige sessão (qualquer papel exceto viewer). Sem login só
    se acede ao match e ao detalhe de UM aviso específico (grants_detail, esse sim público) —
    nunca à listagem completa. Filtros opcionais:
      ?q=                                               (pesquisa em código do aviso + título)
      ?cae=                                             (CAE elegível — regra do prefixo: '55***' casa '55849')
      ?region=                                          (região elegível OU área abrangida)
      ?dimension=micro|pequena|media|grande            (repetível/CSV — avisos que admitem alguma delas)
      ?active=true|false|all
      ?publication_from=YYYY-MM-DD & ?publication_to=  (data de publicação)
      ?closing_from= & ?closing_to=                    (data final)
      ?allocation_min= & ?allocation_max=              (dotação)
      ?rate_min= & ?rate_max=                          (taxa de financiamento)
      ?order_by=                                       (ver _ORDER_BY; default publication_recent)
      ?page= & ?page_size=                             (paginação, default 50, máx. 200)
    A ordenação é feita sobre TODOS os avisos que passam nos filtros, não só na página
    devolvida — o 1º resultado é sempre o de maior dotação/taxa (ou data mais próxima/recente)
    do conjunto inteiro, independentemente de estar ou não na 1ª página por inserção na BD.
    A pesquisa (`q`) filtra o QUERYSET INTEIRO no SQL, antes da paginação — pesquisa em TODOS
    os avisos, não só na página devolvida. CAE/região/dimensão reutilizam as MESMAS regras de
    elegibilidade do motor de match (prefixo de CAE, dimensões admitidas, região/área abrangida);
    um aviso SEM restrição no critério filtrado casa com qualquer valor (é aberto a todos)."""
    qs = Grant.objects.filter(ai_processed=True).prefetch_related(
        "phases", "phase_areas", "financing_rates", "covered_areas",
    )
    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(grant_code__icontains=q) | Q(title__icontains=q))

    active = (request.GET.get("active") or "true").lower()
    if active == "true":
        qs = qs.filter(active=True)
    elif active == "false":
        qs = qs.filter(active=False)

    allocation_min = _float_param(request, "allocation_min")
    allocation_max = _float_param(request, "allocation_max")
    if allocation_min is not None:
        qs = qs.filter(total_allocation__gte=allocation_min)
    if allocation_max is not None:
        qs = qs.filter(total_allocation__lte=allocation_max)

    pub_from, pub_to = parse_date(request.GET.get("publication_from")), parse_date(request.GET.get("publication_to"))
    close_from, close_to = parse_date(request.GET.get("closing_from")), parse_date(request.GET.get("closing_to"))
    rate_min = _float_param(request, "rate_min")
    rate_max = _float_param(request, "rate_max")
    # Filtros de elegibilidade (aplicados em Python, com as regras do match).
    cae = (request.GET.get("cae") or "").strip()
    region = _normalize(request.GET.get("region"))
    dimensions = _dimension_params(request)
    today = date.today()

    rows = []
    for grant in qs:
        if not _in_date_range(grant.publication_date, pub_from, pub_to):
            continue
        if not _in_date_range(grant.closing_date, close_from, close_to):
            continue
        if cae and not _cae_matches(grant, cae):
            continue
        if region and not _region_matches(grant, region):
            continue
        if dimensions and not _dimensions_match(grant, dimensions):
            continue
        rate = _financing_rate(grant)
        if rate_min is not None and (rate is None or rate < rate_min):
            continue
        if rate_max is not None and (rate is None or rate > rate_max):
            continue
        nearest = _nearest_phase_date(grant, today)
        rows.append({
            "pub": parse_date(grant.publication_date),
            "closing": parse_date(grant.closing_date),
            "allocation": grant.total_allocation,
            "rate": rate,
            "summary": _grant_summary(grant, rate, nearest),
        })

    key_fn, reverse = _ORDER_BY.get(request.GET.get("order_by"), _ORDER_BY["publication_recent"])
    # Separa quem tem valor (ordena por ele, na direção pedida) de quem não tem — os sem
    # valor vão SEMPRE para o fim, nunca primeiro, seja qual for a direção da ordenação.
    with_value = sorted((r for r in rows if key_fn(r) is not None), key=key_fn, reverse=reverse)
    without_value = [r for r in rows if key_fn(r) is None]
    grants = [r["summary"] for r in with_value + without_value]

    # Paginação (?page, ?page_size): a lista já está filtrada/ordenada em Python (datas em
    # texto livre, taxa calculada), por isso pagina-se o resultado final, não o queryset —
    # mas SEMPRE depois de ordenar o conjunto completo (ver comentário de _ORDER_BY acima).
    payload = paginate(request, grants, serialize=lambda g: g, items_key="grants")
    return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})


def grants_detail(request, pk):
    """Detalhe COMPLETO de um aviso — todos os campos + relações.

    Autenticados veem qualquer aviso (público entre sessões válidas). Sem sessão, só o(s)
    aviso(s) que o match desta MESMA sessão devolveu (ver common/session_access.py) — impede
    ver avisos arbitrários só por trocar o id na URL. Devolve 404 em ambos os casos de recusa
    (aviso inexistente ou sem acesso) para não revelar se o id existe.
    """
    if not request.user.is_authenticated and not can_view_grant(request, pk):
        return JsonResponse({"error": "Aviso não encontrado."}, status=404)
    grant = Grant.objects.filter(pk=pk).prefetch_related(
        "phases", "covered_areas", "phase_areas", "financing_rates", "expense_limits",
        "non_compliance_penalties", "evaluation_methodologies", "beneficiaries_by_action",
        "documents",
    ).first()
    if grant is None:
        return JsonResponse({"error": "Aviso não encontrado."}, status=404)
    return JsonResponse(
        _grant_detail(grant),
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


# Coleções-filhas editáveis pela MESMA rota /edit/ (chave no body -> como as tratar).
# A `financing_rate` mostrada é CALCULADA destas linhas, não é um campo do aviso; por isso
# são tratadas como substituição total da coleção (o front-end reenvia a lista completa que
# leu no detalhe), enquanto os restantes campos são pares chave->valor no próprio aviso.
_FINANCING_RATE_FIELDS = (
    "company_size", "aid_regime", "base_rate", "regional_bonus", "max_global_rate",
    "minimis_accumulation_limit", "specific_condition",
)


def _build_financing_rate(grant, item):
    return FinancingRate(grant=grant, **{f: item.get(f) for f in _FINANCING_RATE_FIELDS})


def _build_phase_area(grant, item):
    pa = PhaseArea(
        grant=grant,
        fund_name=item.get("fund_name"),
        budget_allocation=item.get("budget_allocation"),
        max_financing_rate=item.get("max_financing_rate"),
        distribution=item.get("distribution") or [],
    )
    # FKs opcionais para a fase/área — têm de pertencer a ESTE aviso (senão 400).
    phase_id, area_id = item.get("phase_id"), item.get("area_id")
    if phase_id is not None:
        if not grant.phases.filter(pk=phase_id).exists():
            raise ValueError(f"phase_id {phase_id} não pertence a este aviso")
        pa.phase_id = phase_id
    if area_id is not None:
        if not grant.covered_areas.filter(pk=area_id).exists():
            raise ValueError(f"area_id {area_id} não pertence a este aviso")
        pa.area_id = area_id
    return pa


# chave do body -> (modelo, related_name, builder, rótulo p/ auditoria)
_COLLECTION_EDITORS = {
    "financing_rates": (FinancingRate, "financing_rates", _build_financing_rate, "taxas de financiamento"),
    "phase_areas": (PhaseArea, "phase_areas", _build_phase_area, "dotações por fase/área"),
}


def _prepare_collection_rows(grant, key, items):
    """Valida os `items` de uma coleção e devolve as instâncias prontas a gravar.
    Levanta ValidationError/ValueError (→ 400) sem tocar na BD. Não grava — só constrói."""
    model, related, builder, _label = _COLLECTION_EDITORS[key]
    if not isinstance(items, list):
        raise ValueError(f"'{key}' tem de ser uma lista de linhas")
    rows = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"'{key}' linha {i}: esperado um objeto JSON")
        row = builder(grant, item)
        # Valida tipos/limites de cada linha; exclui FKs (já resolvidas no builder).
        row.full_clean(exclude=["grant", "phase", "area"], validate_unique=False)
        rows.append(row)
    return model, related, rows


@csrf_exempt
@require_http_methods(["PUT", "PATCH"])
@require_role(*_EDIT_ROLES)
def grants_edit(request, pk):
    """Atualiza um aviso (por id, inalterável) — SÓ admin e commercial. PUT/PATCH.

    Um único pedido altera TUDO de uma vez, atomicamente:
      • campos do aviso — {campo: valor, ...} (fora da whitelist → `ignored`);
      • coleções-filhas — `financing_rates` e/ou `phase_areas` (lista completa que substitui
        a atual). A `financing_rate` mostrada é recalculada destas linhas.
    Notifica os comerciais por email quando algo muda de facto."""
    grant = Grant.objects.filter(pk=pk).first()
    if grant is None:
        return JsonResponse({"error": "Aviso não encontrado."}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Corpo JSON inválido."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Esperado um objeto JSON de campos a alterar."}, status=400)

    # Separa as coleções-filhas dos campos escalares do aviso.
    collection_keys = [k for k in data if k in _COLLECTION_EDITORS]
    scalar_keys = [k for k in data if k not in _COLLECTION_EDITORS]
    updated = [f for f in scalar_keys if f in _EDITABLE_FIELDS]
    ignored = [f for f in scalar_keys if f not in _EDITABLE_FIELDS]

    # Aplica os campos escalares ao objeto (ainda sem gravar) e regista antigo -> novo.
    changes = {}
    for f in updated:
        old = getattr(grant, f, None)
        if old != data[f]:
            changes[f] = (old, data[f])
        setattr(grant, f, data[f])

    # Qualquer escrita por este endpoint (escalares OU coleções) é uma edição MANUAL — marca a
    # origem e quem a fez, para se distinguir do que o pipeline de scrape/IA grava.
    is_write = bool(updated or collection_keys)
    if is_write:
        grant.last_update_source = Grant.SOURCE_MANUAL
        grant.last_updated_by = request.user

    try:
        # Valida campos escalares alterados + constrói as linhas das coleções (sem tocar na BD).
        if updated:
            grant.full_clean(
                exclude=[f.name for f in Grant._meta.fields if f.name not in updated],
                validate_unique=False,
            )
        prepared = {key: _prepare_collection_rows(grant, key, data[key]) for key in collection_keys}
        # Aplica tudo numa transação: escalares + substituição de cada coleção.
        with transaction.atomic():
            if is_write:
                grant.save()
            for model, related, rows in prepared.values():
                getattr(grant, related).all().delete()
                model.objects.bulk_create(rows)
    except ValidationError as e:
        return JsonResponse(
            {"error": "Valores inválidos.", "details": e.message_dict}, status=400,
            json_dumps_params={"ensure_ascii": False},
        )
    except (IntegrityError, DataError, ValueError, TypeError) as e:
        return JsonResponse(
            {"error": f"Não foi possível gravar as alterações: {e}"}, status=400,
            json_dumps_params={"ensure_ascii": False},
        )

    _log_and_notify_edit(request, grant, changes, collection_keys)

    result = {
        "id": grant.id, "grant_code": grant.grant_code,
        "updated": updated, "ignored": ignored,
        "collections_updated": collection_keys,
    }
    # A taxa mostrada pode ter mudado — por edição direta do override ou por alteração das
    # linhas-filhas. Devolve-a efetiva (override manual ou recalculada).
    if collection_keys or "financing_rate" in updated:
        result["financing_rate"] = _financing_rate(grant)
    return JsonResponse(result, json_dumps_params={"ensure_ascii": False, "indent": 2})


def _log_and_notify_edit(request, grant, changes, collection_keys) -> None:
    """Auditoria (quem alterou o quê) + email aos comerciais quando algo muda de facto."""
    if not changes and not collection_keys:
        audit_logger.info(
            "EDIÇÃO aviso %s (id=%s) por %s: sem alterações efetivas (valores iguais).",
            grant.grant_code or "?", grant.pk, request.user.username,
        )
        return
    parts = [f"{f}: {_audit_value(old)} -> {_audit_value(new)}" for f, (old, new) in changes.items()]
    parts += [f"[{_COLLECTION_EDITORS[k][3]} substituídas]" for k in collection_keys]
    audit_logger.info(
        "EDIÇÃO aviso %s (id=%s) por %s: %s",
        grant.grant_code or "?", grant.pk, request.user.username, " | ".join(parts),
    )
    # Comerciais recebem email das alterações (best-effort — não bloqueia a resposta).
    # Uma edição manual é sempre uma ATUALIZAÇÃO (o aviso já existia).
    notify_grants([], [grant])


# --- Scrape (ingestão de avisos) — ABERTO (sem autenticação), POST (tem efeitos) -----

@csrf_exempt
@require_POST
def grants_all(request):
    try:
        data = service.scrape_todos()
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except Exception as e:
        logger.exception("Erro no scrape (todos)")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def grants_compete(request):
    try:
        data = service.scrape_compete()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        logger.exception("Erro no scrape (compete)")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def grants_portugal(request):
    try:
        data = service.scrape_portugal()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        logger.exception("Erro no scrape (portugal)")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def grants_prr(request):
    try:
        data = service.scrape_prr()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        logger.exception("Erro no scrape (prr)")
        return JsonResponse({"error": str(e)}, status=500)
