"""Endpoints da app calls (Django puro + JsonResponse, como as restantes apps)."""

import json
import logging

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from common.pagination import paginate
from users.models import UserProfile
from users.permissions import require_role
from . import services
from .models import Call, CallType, ProgrammeType

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("calls.audit")

# Campos que um colaborador pode corrigir à mão em /calls/<id>/edit/. Fica de fora o bloco
# inteiro de campos CRUS/técnicos da API (api_version, checksum, es_*, ccm_tags, raw,
# raw_fields...) — não têm valor a serem editados manualmente (nunca aparecem no ecrã de
# detalhe como algo para corrigir) e seriam repostos pelo sync seguinte de qualquer forma.
# `call_type`/`programme` (FK) também ficam fora: não há como o payload identificar
# univocamente o catálogo certo sem reintroduzir a lógica de resolução do sync.
_EDITABLE_FIELDS = frozenset({
    "title", "status", "call_identifier", "call_title", "start_date", "deadline_date",
    "deadlines", "deadline_model", "programme_period", "url", "description", "keywords",
    "budget_total", "min_contribution", "max_contribution", "expected_grants",
    "types_of_action", "cross_cutting_priorities", "destination", "destination_group",
    "mission", "geographical_zones", "contract_type", "specific_objective",
    "programme_divisions", "tags", "documents", "project_name", "project_acronym",
    "project_id", "duration", "ca_name", "allow_partner_search",
})


@csrf_exempt
@require_http_methods(["POST"])
def sync_calls(request):
    """POST /calls/sync/ — importa as calls do Funding & Tenders. Rota da automação (n8n).

    POST (e não GET) porque escreve na base de dados. SEM autenticação nem token — qualquer
    pedido POST a esta URL dispara o sync (decisão explícita: fica mais simples de chamar do
    n8n/manualmente, à custa de a rota ficar aberta a quem souber o endereço).
    `csrf_exempt` porque quem chama é um agendador sem sessão nem cookie CSRF.

    Devolve o resumo do que mudou: {"pages", "fetched", "created", "updated", "unchanged",
    "programmes", "types"}.
    """
    try:
        summary = services.sync_calls()
    except services.CallsSyncError as exc:
        logger.warning("Sincronização de calls falhou: %s", exc)
        return JsonResponse({"success": False, "error": str(exc)}, status=502)
    return JsonResponse(
        {"success": True, **summary}, json_dumps_params={"ensure_ascii": False, "indent": 2}
    )


@require_http_methods(["GET"])
@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)
def list_calls(request):
    """GET /calls/ — listagem PAGINADA das calls.

    Filtros: `?status=` (31094502 Open / 31094501 Forthcoming), `?programme=` (código do
    programa-quadro), `?type=` (1/2/8), `?search=` (título/identificador/entidade),
    `?priority=` (prioridade transversal, ex: AI), `?action_type=` (tipo de ação), `?tag=`,
    `?mission=`, `?destination=`, `?has_budget=true|false` e `?has_documents=true|false`.
    `?order_by=` aceita start_latest (default), start_earliest, deadline_earliest,
    deadline_latest, budget_highest, budget_lowest. `?page`/`?page_size` no envelope uniforme
    das restantes listagens.
    """
    boolean_param = {"true": True, "false": False}.get
    has_budget = request.GET.get("has_budget")
    has_documents = request.GET.get("has_documents")
    queryset = services.list_calls(
        status=request.GET.get("status"),
        programme_code=request.GET.get("programme"),
        type_code=request.GET.get("type"),
        order_by=request.GET.get("order_by"),
        search=(request.GET.get("search") or "").strip() or None,
        priority=request.GET.get("priority"),
        action_type=request.GET.get("action_type"),
        # Só filtra se vier explicitamente "true"/"false"; qualquer outra coisa é ignorada.
        has_budget=boolean_param((has_budget or "").lower()),
        has_documents=boolean_param((has_documents or "").lower()),
        tag=request.GET.get("tag"),
        mission=request.GET.get("mission"),
        destination=request.GET.get("destination"),
    )
    payload = paginate(request, queryset, services.serialize_call, items_key="calls")
    return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})


@require_http_methods(["GET"])
@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)
def call_filters(request):
    """GET /calls/filters/ — opções dos selects do front-end.

    Só devolve programas/tipos que TÊM calls na base de dados: o catálogo completo traz 102
    programas, a maioria sem uma única call aberta, e enchia o select de opções que não
    filtram nada.
    """
    programmes = ProgrammeType.objects.filter(calls__isnull=False).distinct()
    types = CallType.objects.filter(calls__isnull=False).distinct()
    return JsonResponse(
        {
            "statuses": [
                {"code": code, "label": label} for code, label in Call.STATUS_CHOICES
            ],
            "programmes": [
                {"code": p.code, "name": p.name} for p in programmes
            ],
            "types": [{"code": t.code, "name": t.name} for t in types],
            "action_types": services.distinct_action_types(),
            "priorities": services.distinct_priorities(),
            "missions": services.distinct_missions(),
            "destinations": services.distinct_destinations(),
            "tags": services.top_tags(),
        },
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


def _audit_value(value, limit: int = 300) -> str:
    """Representação curta de um valor para o log de auditoria (trunca listas/textos longos)."""
    text_repr = repr(value)
    return text_repr if len(text_repr) <= limit else text_repr[:limit] + "…[truncado]"


@csrf_exempt
@require_http_methods(["PUT", "PATCH"])
@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)
def call_edit(request, pk):
    """PUT/PATCH /calls/<pk>/edit/ — corrige campos de UMA call à mão.

    {campo: valor, ...} — campos fora de `_EDITABLE_FIELDS` são ignorados (devolvidos em
    `ignored`, não em erro). Marca `last_update_source=SOURCE_MANUAL` e `last_updated_by`;
    um sync SEGUINTE sobrescreve o que a API trouxer, tal como o scrape faz a avisos.Grant —
    isto é só o registo de quem tocou por último, não uma trava contra o próximo sync.
    """
    call = Call.objects.filter(pk=pk).first()
    if call is None:
        return JsonResponse({"error": "Call não encontrada."}, status=404)
    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        # UnicodeDecodeError: um corpo que não é sequer UTF-8 válido (json.loads decodifica
        # antes de fazer parse) — sem isto, um payload malformado dava 500 em vez de 400.
        return JsonResponse({"error": "Corpo JSON inválido."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Esperado um objeto JSON de campos a alterar."}, status=400)

    updated = [field_name for field_name in payload if field_name in _EDITABLE_FIELDS]
    ignored = [field_name for field_name in payload if field_name not in _EDITABLE_FIELDS]
    # Auditoria: captura o valor ANTIGO antes de aplicar, para registar antigo -> novo.
    changes = {}
    for field_name in updated:
        old = getattr(call, field_name, None)
        if old != payload[field_name]:
            changes[field_name] = (old, payload[field_name])
        setattr(call, field_name, payload[field_name])

    if updated:
        call.last_update_source = Call.SOURCE_MANUAL
        call.last_updated_by = request.user
        try:
            # Valida SÓ os campos alterados (400 com detalhe, em vez de 500): campos não
            # tocados (incluindo os crus/técnicos, sempre fora da whitelist) não são revalidados.
            call.full_clean(
                exclude=[model_field.name for model_field in Call._meta.fields
                         if model_field.name not in updated],
                validate_unique=False,
            )
            call.save()
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
        if changes:
            audit_logger.info(
                "EDIÇÃO call %s (id=%s) por %s: %s",
                call.identifier or "?", call.pk, request.user.username,
                " | ".join(
                    f"{field_name}: {_audit_value(old)} -> {_audit_value(new)}"
                    for field_name, (old, new) in changes.items()
                ),
            )
        else:
            audit_logger.info(
                "EDIÇÃO call %s (id=%s) por %s: sem alterações efetivas (valores iguais).",
                call.identifier or "?", call.pk, request.user.username,
            )
    return JsonResponse(
        {"id": call.id, "identifier": call.identifier, "updated": updated, "ignored": ignored},
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


@require_http_methods(["GET"])
@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)
def call_detail(request, pk):
    """GET /calls/<pk>/ — detalhe completo: textos longos, orçamento e a metadata crua."""
    call = (
        Call.objects.select_related("call_type", "programme").filter(pk=pk).first()
    )
    if call is None:
        return JsonResponse({"error": "Call not found."}, status=404)
    return JsonResponse(
        services.serialize_call_detail(call),
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )
