"""Autenticação das rotas de automação (n8n e afins).

As rotas de sync/import não são usadas por pessoas com sessão iniciada: são disparadas por
um agendador externo, que não tem cookies nem faz login. Estavam por isso ABERTAS — qualquer
um na internet podia disparar um scrape completo do Portugal 2030 ou um import de milhares de
calls, e usar o servidor como amplificador contra as fontes que ele consulta.

Este decorator fecha-as com um segredo partilhado no header `X-Sync-Token`, comparado com
`SYNC_TOKEN` do .env.

Se `SYNC_TOKEN` não estiver definido, as rotas ficam FECHADAS (503), não abertas: uma variável
esquecida no deploy tem de falhar de forma visível em vez de reabrir em silêncio o buraco que
isto veio tapar.
"""

import hmac
from functools import wraps

from django.conf import settings
from django.http import JsonResponse

SYNC_TOKEN_HEADER = "X-Sync-Token"


def require_sync_token(view):
    """Exige o header `X-Sync-Token` igual a `settings.SYNC_TOKEN`."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        expected = getattr(settings, "SYNC_TOKEN", "") or ""
        if not expected:
            return JsonResponse(
                {"error": "Automation routes are disabled: SYNC_TOKEN is not configured."},
                status=503,
            )
        provided = request.headers.get(SYNC_TOKEN_HEADER, "")
        # compare_digest em vez de '==': a comparação de strings do Python sai no primeiro
        # byte diferente, e esse tempo revela o prefixo correto do token a quem meça.
        if not provided or not hmac.compare_digest(provided, expected):
            return JsonResponse({"error": "Invalid or missing sync token."}, status=401)
        return view(request, *args, **kwargs)

    return wrapped
