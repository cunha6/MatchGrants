"""
Camada FINAL de validação do match por LLM (OpenRouter, modelo gratuito).

Ordem do match: filtro DETERMINÍSTICO (CAE/localização/dimensão/tipo) → pesquisa SEMÂNTICA
(embeddings) → esta camada. Aqui pergunta-se a um LLM, para CADA aviso que passou, se ele é
REALMENTE adequado ao perfil do cliente. Apanha o que nem os padrões nem os embeddings
distinguem — ex: um aviso para "entidades gestoras de resíduos urbanos" que passa por omissão
(não restringe CAE/dimensão) mas não serve uma consultora de gestão.

Chave: roda entre OPENROUTER_API_KEY e OPENROUTER_API_KEY1 (ver `_next_api_key`), uma
por validação, para repartir o consumo pelo limite gratuito de cada uma.

Degradação graciosa (NUNCA esconder por acidente): sem chave nenhuma, em falha de
rede/API, timeout, ou resposta ilegível → devolve {} e o match mantém TODOS os avisos que a
semântica devolveu. Só filtra um aviso quando o LLM diz explicitamente que NÃO é adequado.
"""

import json
import logging
import threading

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Rotação das chaves do OpenRouter (OPENROUTER_API_KEY e OPENROUTER_API_KEY1 do .env): a
# cada validação usa-se a seguinte (round-robin), para repartir os pedidos e não esgotar o
# limite gratuito de uma só chave. Índice global ao processo e protegido por lock (seguro
# entre as threads do gunicorn). Mesmo padrão da rotação das chaves nif.pt.
_key_lock = threading.Lock()
_key_index = 0


def _next_api_key() -> str | None:
    """Chave OpenRouter seguinte na rotação.

    Lê `settings.OPENROUTER_API_KEYS` a cada chamada (respeita `override_settings` nos
    testes). Sem lista, cai para a chave única `OPENROUTER_API_KEY`; None se não houver
    nenhuma configurada — nesse caso a validação é simplesmente saltada.
    """
    global _key_index
    keys = list(getattr(settings, "OPENROUTER_API_KEYS", None) or [])
    if not keys:
        return getattr(settings, "OPENROUTER_API_KEY", None)
    with _key_lock:
        key = keys[_key_index % len(keys)]
        _key_index += 1
    return key


_URL = "https://openrouter.ai/api/v1/chat/completions"
_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
# O modelo é gratuito, "reasoning" (pensa antes de responder) e pode ser lento MESMO quando
# corre bem — medido ~45s numa chamada bem-sucedida. 60s dá folga sem deixar o pior caso
# (_MAX_ATTEMPTS tentativas) disparar para minutos.
_TIMEOUT = 60
_MAX_CHARS = 1200        # truncagem de campos longos (objetivo, ações, critérios)
_MAX_LIST_ITEMS = 20     # nº máx. de itens por lista enviada
# Tentativas antes de desistir e degradar (ver validate_matches). O modelo gratuito falha
# com frequência de forma TRANSITÓRIA (rate-limit da Nvidia, resposta ilegível/vazia) — cada
# tentativa roda para a chave OpenRouter seguinte, por isso também ajuda a contornar uma
# chave momentaneamente esgotada. Só 2 (não mais): dado que o modelo já é lento mesmo em
# sucesso (~45s), cada tentativa a mais empurra o pior caso para perto do que um utilizador
# aceita esperar — 2×60s (~2min) é o limite razoável, contra os ~4,5min de 3×90s.
_MAX_ATTEMPTS = 2

_SYSTEM = (
    "És um avaliador de elegibilidade de apoios/financiamentos públicos. Recebes o PERFIL de "
    "uma empresa/entidade e uma LISTA de avisos que JÁ passaram um filtro automático "
    "(CAE, localização, dimensão, tipo de beneficiário) e uma pesquisa semântica.\n\n"
    "Tarefa: para CADA aviso, decidir se é REALMENTE adequado às características do cliente — "
    "olha para o setor/atividade do cliente vs o objetivo/ações/setores-alvo do aviso, e para "
    "o tipo de entidade/destinatários que o aviso exige (ex: 'entidades gestoras de resíduos', "
    "'municípios', 'instituições de ensino').\n\n"
    "Sê CONSERVADOR: marca \"adequate\": false SÓ quando é CLARAMENTE desadequado (ex: uma "
    "consultora de gestão para um aviso de infraestruturas de resíduos urbanos destinado a "
    "entidades gestoras de resíduos). NA DÚVIDA, marca \"adequate\": true — não escondas um "
    "aviso só por incerteza.\n\n"
    "O campo \"reason\" só interessa quando \"adequate\": true (os avisos não adequados são "
    "removidos da lista final e o \"reason\" deles nunca é mostrado a ninguém — não percas "
    "tempo a justificá-los, usa \"reason\": null).\n"
    "Quando \"adequate\": true, em vez de confirmar genericamente, diz CONCRETAMENTE que tipo "
    "de investimento/projeto a empresa pode desenvolver no âmbito deste aviso, ligando a "
    "atividade real dela ao objetivo/ações elegíveis. Nunca uma frase vaga como \"é adequado\" "
    "ou \"sim\" sozinha.\n"
    "Estilo do \"reason\": OBJETIVO e direto — 1 frase, máx. ~25 palavras, sem introduções "
    "(\"a empresa pode...\", \"considerando que...\"), sem repetir o nome da empresa nem do "
    "aviso, direto ao tipo de investimento. Ex.: \"Digitalização dos processos de "
    "consultadoria e implementação de novas ferramentas de gestão para clientes.\"\n\n"
    "Responde APENAS com um array JSON, um objeto por aviso, exatamente nesta forma:\n"
    "[{\"id\": <id do aviso, tal como recebido>, \"adequate\": true|false, "
    "\"reason\": \"<ver regra acima, ou null se adequate=false>\"}]\n"
    "Sem texto antes nem depois do array. Inclui TODOS os avisos recebidos."
)


def _trunc(value, limit: int = _MAX_CHARS):
    """Trunca uma string longa (mantém o payload do LLM manejável)."""
    if value is None:
        return None
    text = str(value).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _trunc_list(value):
    """Lista com itens truncados e limitada a _MAX_LIST_ITEMS."""
    if not value:
        return []
    return [_trunc(list_entry, 400) for list_entry in list(value)[:_MAX_LIST_ITEMS] if list_entry]


def _client_payload(md: dict) -> dict:
    """Só os campos do cliente que caracterizam a adequação (sem dados de contacto/burocracia)."""
    return {
        "entity_type": md.get("entity_type"),
        "entity_size": md.get("dimension"),
        "main_cae": md.get("main_cae"),
        "secondary_cae": md.get("secondary_cae"),
        "region": md.get("region"),
        "nuts_ii": md.get("nuts_ii"),
        "nuts_iii": md.get("nuts_iii"),
        "nature": md.get("nature_label") or md.get("nature"),
        "activity": _trunc(md.get("activity")),
        "city": md.get("city"),
        "county": md.get("county"),
    }


def _grant_payload(grant) -> dict:
    """Só os campos do aviso que descrevem o que financia e para quem (para o LLM decidir)."""
    return {
        "id": grant.id,
        "grant_code": grant.grant_code,
        "title": _trunc(grant.title),
        "objective": _trunc(grant.objective),
        "specific_objective": _trunc(grant.specific_objective),
        "covered_actions": _trunc(grant.covered_actions),
        "included_caes": grant.included_caes or [],
        "excluded_caes": grant.excluded_caes or [],
        "eligible_regions": grant.eligible_regions or [],
        "target_technology_sectors": grant.target_technology_sectors or [],
        "beneficiary_eligibility_criteria": _trunc_list(grant.beneficiary_eligibility_criteria),
        "operation_eligibility_criteria": _trunc_list(grant.operation_eligibility_criteria),
        "final_recipients": grant.final_recipients or [],
        # geographic_area de cada CoveredArea (pré-carregado por prefetch no match).
        "covered_areas": [area.geographic_area for area in grant.covered_areas.all() if area.geographic_area],
    }


def _parse_verdicts(content: str) -> dict[int, dict]:
    """Extrai {grant_id: {adequate, reason}} da resposta do LLM (tolerante a lixo à volta)."""
    if not content:
        return {}
    parsed_payload = _loads(content.strip())
    if parsed_payload is None:  # tenta extrair o bloco JSON de dentro de texto/markdown
        for opener, closer in (("[", "]"), ("{", "}")):
            start_index, end_index = content.find(opener), content.rfind(closer)
            if start_index != -1 and end_index > start_index:
                parsed_payload = _loads(content[start_index:end_index + 1])
                if parsed_payload is not None:
                    break
    if parsed_payload is None:
        return {}
    items = parsed_payload.get("results") if isinstance(parsed_payload, dict) else parsed_payload
    if not isinstance(items, list):
        return {}
    out: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            gid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        reason = it.get("reason")
        out[gid] = {
            "adequate": bool(it.get("adequate")),
            "reason": str(reason).strip() if reason is not None else None,
        }
    return out


def _loads(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _request_verdicts(key: str, payload: dict) -> dict[int, dict] | None:
    """Uma ÚNICA tentativa. None em qualquer falha (rede, resposta sem conteúdo utilizável,
    ou conteúdo ilegível) — quem chama decide se vale a pena tentar de novo."""
    body = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    try:
        http_response = requests.post(
            _URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-Title": "MatchGrants",
            },
            data=json.dumps(body),
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Validação LLM (OpenRouter) falhou (rede): %s", exc)
        return None

    # Corpo CRU da resposta (o que o OpenRouter devolveu) — logado SEMPRE, mesmo em erro, para
    # inspeção. É aqui que se vê um erro do OpenRouter (rate-limit, modelo indisponível…) que de
    # outra forma só aparecia como um KeyError sem contexto.
    raw = http_response.text
    logger.info("Validação LLM (OpenRouter) [%s] HTTP %s — corpo cru:\n%s",
                _MODEL, http_response.status_code, raw[:4000])

    try:
        parsed_payload = http_response.json()
        content = parsed_payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("Validação LLM (OpenRouter) sem resposta utilizável (%s).", type(exc).__name__)
        return None

    verdicts = _parse_verdicts(content or "")
    if not verdicts:
        logger.warning("Validação LLM: resposta ilegível.")
        return None
    return verdicts


def validate_matches(client_metadata: dict, grants: list) -> dict[int, dict]:
    """{grant_id: {"adequate": bool, "reason": str}} para os avisos `grants`, via OpenRouter.

    {} (nada validado → nada filtrado) se: não houver chave configurada, a lista for vazia,
    ou TODAS as tentativas falharem. O modelo gratuito falha com frequência de forma
    TRANSITÓRIA (rate-limit, resposta ilegível) — em vez de desistir à primeira, tenta até
    `_MAX_ATTEMPTS` vezes, rodando para a chave OpenRouter seguinte a cada tentativa.
    """
    if not grants:
        return {}
    key = _next_api_key()
    if not key:
        return {}

    payload = {
        "client": _client_payload(client_metadata),
        "grants": [_grant_payload(grant_obj) for grant_obj in grants],
    }

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if attempt > 1:
            key = _next_api_key()
            if not key:
                break
        verdicts = _request_verdicts(key, payload)
        if verdicts is not None:
            return verdicts
        if attempt < _MAX_ATTEMPTS:
            logger.info("Validação LLM: tentativa %d/%d falhou — a tentar de novo.",
                       attempt, _MAX_ATTEMPTS)

    logger.warning(
        "Validação LLM: todas as %d tentativas falharam — match segue sem filtro LLM.",
        _MAX_ATTEMPTS)
    return {}
