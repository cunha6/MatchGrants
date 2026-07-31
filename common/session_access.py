"""Sessão anónima: acesso pontual a avisos concretos, concedido pelo match.

Quem não tem login só pode ver o detalhe de um aviso (GET /avisos/<id>/) se esse id apareceu
nos resultados de um match feito pela MESMA sessão (cookie) — impede alterar o id na URL para
ver avisos arbitrários que não fizeram parte do match. Autenticados não passam por aqui (o
detalhe é público para eles, como já era).

Acumula ao longo de vários matches na mesma sessão (nunca substitui): quem avalia NIFs
diferentes continua a ver os avisos de matches anteriores, não só do último.
"""

# Pública (não _prefixada) para os testes poderem pré-semear a sessão sem duplicar o nome
# da chave (ver avisos/tests.py).
SESSION_KEY = "matched_grant_ids"


def allow_grants(request, grant_ids) -> None:
    """Acrescenta `grant_ids` à lista de avisos que esta sessão pode ver em detalhe."""
    ids = {int(i) for i in grant_ids if i is not None}
    if not ids:
        return
    allowed = set(request.session.get(SESSION_KEY, []))
    allowed |= ids
    request.session[SESSION_KEY] = list(allowed)


def can_view_grant(request, grant_id: int) -> bool:
    """True se esta sessão já viu `grant_id` num match (ver `allow_grants`)."""
    return int(grant_id) in set(request.session.get(SESSION_KEY, []))
