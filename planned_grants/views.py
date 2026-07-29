"""Endpoints da app planned_grants (Django puro + JsonResponse, como as restantes apps)."""

import logging

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from common.pagination import paginate
from users.models import UserProfile
from users.permissions import require_role
from . import services

logger = logging.getLogger(__name__)


@require_http_methods(["GET"])
@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)
def list_planned_grants(request):
    """GET /planned-grants/ — listagem PAGINADA dos avisos previstos (Plano Anual) com abertura
    prevista a partir de hoje (os já passados ficam de fora). Exige sessão (admin ou um dos
    comerciais) — o client não tem acesso ao plano anual.

    Ordenados por data de abertura prevista por omissão; `?order_by=` escolhe outra (ver
    services.ORDERING: start_earliest/start_latest, end_earliest/end_latest,
    allocation_highest/allocation_lowest). Query params `?page`/`?page_size` (default 50,
    máx. 200), no envelope uniforme das restantes listagens."""
    qs = services.upcoming_planned_grants(order_by=request.GET.get("order_by"))
    payload = paginate(request, qs, services.serialize_planned_grant, items_key="planned_grants")
    return JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "indent": 2})


@require_http_methods(["GET"])
def sync_planned_grants(request):
    """GET /planned-grants/sync/ — scrape do plano anual, encontra o Excel, descarrega e
    sincroniza a base de dados. Devolve apenas {"success": true} (ou 502 em falha de scrape)."""
    try:
        services.sync_planned_grants()
    except services.PlannedGrantsSyncError as exc:
        logger.warning("Sincronização do plano anual falhou: %s", exc)
        return JsonResponse({"success": False, "error": str(exc)}, status=502)
    return JsonResponse({"success": True})
