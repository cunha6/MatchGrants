"""Endpoints da newsletter (Django puro + JsonResponse, como as restantes apps)."""

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from users.models import UserProfile
from users.permissions import require_role
from . import services


@require_http_methods(["GET"])
@require_role(UserProfile.ADMIN, UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC)
def weekly_news(request):
    """GET /news/weekly/ — dados da newsletter semanal (novos/atualizados avisos e anúncios da
    última semana + avisos previstos para os próximos 30 dias). Exige sessão (admin ou um dos
    comerciais) — o client não tem acesso à newsletter.
    A ordenação, pesquisa, filtros e paginação são feitos no front-end sobre as listas devolvidas."""
    return JsonResponse(
        services.weekly_newsletter(),
        json_dumps_params={"ensure_ascii": False, "indent": 2},
    )
