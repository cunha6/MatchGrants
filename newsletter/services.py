"""Agregação de conteúdos para a newsletter semanal.

Cruza várias fontes (não pertence a nenhuma app em particular): novidades da última semana em
`avisos.Grant` e `anuncios.Notice`, e os avisos PREVISTOS (`planned_grants.PlannedGrant`) a abrir
nos próximos 30 dias. Reutiliza os serializers já existentes de cada entidade — nenhuma lógica de
serialização é duplicada aqui.

Devolve as LISTAS COMPLETAS da semana (um conjunto pequeno e limitado); a ordenação, a pesquisa
na descrição, os filtros (tipo de ato/contrato) e a paginação são feitos no FRONT-END. Por isso
cada item traz todos os campos necessários para isso: os avisos incluem título/objetivo/prazo/
dotação; os anúncios incluem preço/prazo/descrição/act_type/contract_types.
"""

from datetime import timedelta

from django.utils import timezone

from anuncios.models import Notice
from anuncios.services import serialize_notice
from avisos.models import Grant
from avisos.serializers import grant_detail
from planned_grants.services import serialize_planned_grant, upcoming_planned_grants

_RECENT_DAYS = 7      # "novo/atualizado" = mexido nos últimos 7 dias
_UPCOMING_DAYS = 30   # "próximos avisos" = previstos para abrir nos próximos 30 dias


def weekly_newsletter() -> dict:
    """Dados da newsletter semanal, prontos para JSON.

    Secções:
      - `new_grants` / `new_notices`: criados nos últimos 7 dias;
      - `updated_grants` / `updated_notices`: atualizados nos últimos 7 dias mas criados
        antes disso (alterações, não novidades);
      - `coming_next_30_days`: avisos previstos no plano anual a abrir nos próximos 30 dias.
    """
    now = timezone.now()
    week_ago = now - timedelta(days=_RECENT_DAYS)
    today = timezone.localdate()
    horizon = today + timedelta(days=_UPCOMING_DAYS)

    new_grants = Grant.objects.filter(created_at__gte=week_ago)
    updated_grants = Grant.objects.filter(updated_at__gte=week_ago, created_at__lt=week_ago)
    new_notices = Notice.objects.filter(created_at__gte=week_ago)
    updated_notices = Notice.objects.filter(updated_at__gte=week_ago, created_at__lt=week_ago)
    coming = upcoming_planned_grants().filter(expected_start__lte=horizon)

    return {
        "generated_at": now.isoformat(),
        "new_grants": [grant_detail(grant) for grant in new_grants],
        "updated_grants": [grant_detail(grant) for grant in updated_grants],
        "new_notices": [serialize_notice(notice) for notice in new_notices],
        "updated_notices": [serialize_notice(notice) for notice in updated_notices],
        "coming_next_30_days": [serialize_planned_grant(pg) for pg in coming],
    }
