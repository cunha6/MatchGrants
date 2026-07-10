import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from users.models import UserProfile
from users.permissions import require_role
from . import service
from .models import Grant

# Editar um aviso (endpoint próprio) — só ADMIN e COMMERCIAL.
_EDIT_ROLES = (UserProfile.ADMIN, UserProfile.COMMERCIAL)

# "Tudo menos o id": qualquer campo próprio do aviso pode ser editado, exceto a chave primária.
_EDITABLE_FIELDS = frozenset(f.name for f in Grant._meta.fields if not f.primary_key)


def _grant_summary(g: Grant) -> dict:
    """Resumo de um aviso para a listagem (leitura)."""
    return {
        "id": g.id,
        "grant_code": g.grant_code,
        "title": g.title,
        "financing_program": g.financing_program,
        "notice_modality": g.notice_modality,
        "opening_date": g.opening_date,
        "closing_date": g.closing_date,
        "active": g.active,
        "eligible_regions": g.eligible_regions,
        "total_allocation": g.total_allocation,
        "scraping_url": g.scraping_url,
    }


def grants_list(request):
    """Lista os avisos da BD — LEITURA PÚBLICA (vista por todos).
    Filtro opcional ?active=true (por defeito) | false | all."""
    qs = Grant.objects.filter(ai_processed=True).order_by("-updated_at")
    active = (request.GET.get("active") or "true").lower()
    if active == "true":
        qs = qs.filter(active=True)
    elif active == "false":
        qs = qs.filter(active=False)
    grants = [_grant_summary(g) for g in qs]
    return JsonResponse(
        {"total": len(grants), "grants": grants},
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


@csrf_exempt
@require_http_methods(["POST", "PUT", "PATCH"])
@require_role(*_EDIT_ROLES)
def grants_edit(request, pk):
    """Edita os campos de conteúdo de um aviso — SÓ admin e commercial.
    POST/PUT/PATCH {campo: valor, ...}. Campos fora da whitelist são ignorados."""
    grant = Grant.objects.filter(pk=pk).first()
    if grant is None:
        return JsonResponse({"error": "Aviso não encontrado."}, status=404)
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Corpo JSON inválido."}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Esperado um objeto JSON de campos a alterar."}, status=400)

    updated = [f for f in data if f in _EDITABLE_FIELDS]
    ignored = [f for f in data if f not in _EDITABLE_FIELDS]
    for f in updated:
        setattr(grant, f, data[f])
    if updated:
        grant.save()
    return JsonResponse(
        {"id": grant.id, "grant_code": grant.grant_code, "updated": updated, "ignored": ignored},
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )


# --- Scrape (ingestão de avisos) — ABERTO, sem autenticação -----------------

def grants_all(request):
    try:
        data = service.scrape_todos()
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def grants_compete(request):
    try:
        data = service.scrape_compete()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def grants_portugal(request):
    try:
        data = service.scrape_portugal()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def grants_prr(request):
    try:
        data = service.scrape_prr()
        return JsonResponse(
            {"total": len(data), "grants": data},
            json_dumps_params={"ensure_ascii": False, "indent": 2},
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
