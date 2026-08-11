"""Endpoints da app avisos: listagem (exige sessão), detalhe (autenticados: qualquer aviso;
sem sessão: só os avisos que o match dessa sessão devolveu — ver common/session_access.py),
edição (admin+commercial_grants+commercial_public) e scrape (aberto, para automação diária).
As views são magras — a lógica vive em service/db."""

import json
import logging
import mimetypes
import os

from django.core.exceptions import ValidationError
from django.db import DataError, IntegrityError, transaction
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from common.files import safe_media_path
from common.pagination import paginate
from common.session_access import can_view_grant
from users.models import UserProfile
from users.permissions import require_role
from . import service
from .models import FinancingRate, Grant, PhaseArea
from .listing import list_grants
from .serializers import (
    _AVISOS_DIR, financing_rate, grant_detail,
)
from .notifications import notify_grants

logger = logging.getLogger(__name__)
# Trilho de auditoria das edições de avisos (quem alterou o quê) → consola + logs/avisos.log.
audit_logger = logging.getLogger("avisos.audit")


def _audit_value(value, limit: int = 300) -> str:
    """Representação curta de um valor para o log de auditoria (trunca listas/textos longos)."""
    text_repr = repr(value)
    return text_repr if len(text_repr) <= limit else text_repr[:limit] + "…[truncado]"

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
    model_field.name for model_field in Grant._meta.fields
    if not model_field.primary_key and model_field.name not in _NON_EDITABLE_FIELDS
)

def serve_grant_document(request, pk):
    """Serve o PDF do aviso (pdf_Avisos) para abrir no browser (inline).

    Mesma política de acesso do detalhe (`grants_detail`): autenticados veem qualquer aviso;
    sem sessão, só os avisos que o match desta sessão devolveu — senão o PDF continuaria
    enumerável por id, contornando o gate do detalhe. O front-end liga a este URL com
    target="_blank"."""
    if not request.user.is_authenticated and not can_view_grant(request, pk):
        return JsonResponse({"error": "Aviso não encontrado."}, status=404)
    grant = Grant.objects.filter(pk=pk).only("id", "pdf_path").first()
    if grant is None:
        return JsonResponse({"error": "Aviso não encontrado."}, status=404)
    path = safe_media_path(grant.pdf_path, _AVISOS_DIR)
    if not path:
        return JsonResponse({"error": "Documento do aviso não disponível."}, status=404)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(open(path, "rb"), content_type=ctype,
                        as_attachment=False, filename=os.path.basename(path))


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
      ?order_by=                                       (ver listing._ORDER_BY; default publication_recent)
      ?page= & ?page_size=                             (paginação, default 50, máx. 200)
    A ordenação é feita sobre TODOS os avisos que passam nos filtros, não só na página
    devolvida — o 1º resultado é sempre o de maior dotação/taxa (ou data mais próxima/recente)
    do conjunto inteiro, independentemente de estar ou não na 1ª página por inserção na BD.
    A pesquisa (`q`) filtra o QUERYSET INTEIRO no SQL, antes da paginação — pesquisa em TODOS
    os avisos, não só na página devolvida. CAE/região/dimensão reutilizam as MESMAS regras de
    elegibilidade do motor de match (prefixo de CAE, dimensões admitidas, região/área abrangida);
    um aviso SEM restrição no critério filtrado casa com qualquer valor (é aberto a todos)."""
    # Filtragem/ordenação vivem em listing.py (lógica de negócio, não de HTTP).
    grants = list_grants(request.GET)
    # Paginação (?page, ?page_size): a lista já está filtrada/ordenada em Python (datas em
    # texto livre, taxa calculada), por isso pagina-se o resultado final, não o queryset —
    # mas SEMPRE depois de ordenar o conjunto completo (ver listing._sort).
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
        grant_detail(grant),
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


def _build_financing_rate(grant, row_payload):
    return FinancingRate(grant=grant, **{field_name: row_payload.get(field_name) for field_name in _FINANCING_RATE_FIELDS})


def _build_phase_area(grant, row_payload):
    phase_area = PhaseArea(
        grant=grant,
        fund_name=row_payload.get("fund_name"),
        budget_allocation=row_payload.get("budget_allocation"),
        max_financing_rate=row_payload.get("max_financing_rate"),
        distribution=row_payload.get("distribution") or [],
    )
    # FKs opcionais para a fase/área — têm de pertencer a ESTE aviso (senão 400).
    phase_id, area_id = row_payload.get("phase_id"), row_payload.get("area_id")
    if phase_id is not None:
        if not grant.phases.filter(pk=phase_id).exists():
            raise ValueError(f"phase_id {phase_id} não pertence a este aviso")
        phase_area.phase_id = phase_id
    if area_id is not None:
        if not grant.covered_areas.filter(pk=area_id).exists():
            raise ValueError(f"area_id {area_id} não pertence a este aviso")
        phase_area.area_id = area_id
    return phase_area


# chave do body -> (modelo, related_name, builder, rótulo p/ auditoria)
_COLLECTION_EDITORS = {
    "financing_rates": (FinancingRate, "financing_rates", _build_financing_rate, "taxas de financiamento"),
    "phase_areas": (PhaseArea, "phase_areas", _build_phase_area, "dotações por fase/área"),
}


def _prepare_collection_rows(grant, key, items):
    """Valida os `items` de uma coleção e devolve as instâncias prontas a gravar.
    Levanta ValidationError/ValueError (→ 400) sem tocar na BD. Não grava — só constrói."""
    model, related, build_row, _label = _COLLECTION_EDITORS[key]
    if not isinstance(items, list):
        raise ValueError(f"'{key}' tem de ser uma lista de linhas")
    rows = []
    for index, row_payload in enumerate(items):
        if not isinstance(row_payload, dict):
            raise ValueError(f"'{key}' linha {index}: esperado um objeto JSON")
        built_row = build_row(grant, row_payload)
        # Valida tipos/limites de cada linha; exclui FKs (já resolvidas no builder).
        built_row.full_clean(exclude=["grant", "phase", "area"], validate_unique=False)
        rows.append(built_row)
    return model, related, rows


def _parse_edit_body(request) -> dict:
    """Corpo do PUT/PATCH como dicionário de campos a alterar. ValueError (→ 400) se o JSON
    for inválido ou não for um objeto."""
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        raise ValueError("Corpo JSON inválido.")
    if not isinstance(payload, dict):
        raise ValueError("Esperado um objeto JSON de campos a alterar.")
    return payload


def _apply_scalar_fields(grant, payload: dict, updated: list) -> dict:
    """Aplica os campos escalares ao objeto (ainda SEM gravar) e devolve {campo: (antigo,
    novo)} só dos que mudaram de facto — é o que alimenta a auditoria e a notificação."""
    changes = {}
    for field_name in updated:
        old = getattr(grant, field_name, None)
        if old != payload[field_name]:
            changes[field_name] = (old, payload[field_name])
        setattr(grant, field_name, payload[field_name])
    return changes


def _validate_and_persist(grant, payload: dict, updated: list, collection_keys: list,
                          is_write: bool):
    """Valida e grava escalares + coleções numa só transação. Devolve None em sucesso, ou o
    JsonResponse de erro (400) — assim o caminho feliz da view fica sem try/except."""
    try:
        # Valida campos escalares alterados + constrói as linhas das coleções (sem tocar na BD).
        if updated:
            grant.full_clean(
                exclude=[model_field.name for model_field in Grant._meta.fields
                         if model_field.name not in updated],
                validate_unique=False,
            )
        prepared = {key: _prepare_collection_rows(grant, key, payload[key]) for key in collection_keys}
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
    return None


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
        payload = _parse_edit_body(request)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    # Separa as coleções-filhas dos campos escalares do aviso.
    collection_keys = [k for k in payload if k in _COLLECTION_EDITORS]
    scalar_keys = [k for k in payload if k not in _COLLECTION_EDITORS]
    updated = [field_name for field_name in scalar_keys if field_name in _EDITABLE_FIELDS]
    ignored = [field_name for field_name in scalar_keys if field_name not in _EDITABLE_FIELDS]

    changes = _apply_scalar_fields(grant, payload, updated)

    # Qualquer escrita por este endpoint (escalares OU coleções) é uma edição MANUAL — marca a
    # origem e quem a fez, para se distinguir do que o pipeline de scrape/IA grava.
    is_write = bool(updated or collection_keys)
    if is_write:
        grant.last_update_source = Grant.SOURCE_MANUAL
        grant.last_updated_by = request.user

    error = _validate_and_persist(grant, payload, updated, collection_keys, is_write)
    if error is not None:
        return error

    _log_and_notify_edit(request, grant, changes, collection_keys)

    result = {
        "id": grant.id, "grant_code": grant.grant_code,
        "updated": updated, "ignored": ignored,
        "collections_updated": collection_keys,
    }
    # A taxa mostrada pode ter mudado — por edição direta do override ou por alteração das
    # linhas-filhas. Devolve-a efetiva (override manual ou recalculada).
    if collection_keys or "financing_rate" in updated:
        result["financing_rate"] = financing_rate(grant)
    return JsonResponse(result, json_dumps_params={"ensure_ascii": False, "indent": 2})


def _log_and_notify_edit(request, grant, changes, collection_keys) -> None:
    """Auditoria (quem alterou o quê) + email aos comerciais quando algo muda de facto."""
    if not changes and not collection_keys:
        audit_logger.info(
            "EDIÇÃO aviso %s (id=%s) por %s: sem alterações efetivas (valores iguais).",
            grant.grant_code or "?", grant.pk, request.user.username,
        )
        return
    parts = [f"{field_name}: {_audit_value(old)} -> {_audit_value(new)}" for field_name, (old, new) in changes.items()]
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
        payload = service.scrape_todos()
        return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except Exception as e:
        logger.exception("Erro no scrape (todos)")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def grants_compete(request):
    try:
        payload = service.scrape_compete()
        return JsonResponse(
            {"total": len(payload), "grants": payload},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        logger.exception("Erro no scrape (compete)")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def grants_portugal(request):
    try:
        payload = service.scrape_portugal()
        return JsonResponse(
            {"total": len(payload), "grants": payload},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        logger.exception("Erro no scrape (portugal)")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_POST
def grants_prr(request):
    try:
        payload = service.scrape_prr()
        return JsonResponse(
            {"total": len(payload), "grants": payload},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        logger.exception("Erro no scrape (prr)")
        return JsonResponse({"error": str(e)}, status=500)
