"""
Camada FINAL de validação do match por LLM (OpenRouter, modelo gratuito).

Ordem do match: filtro DETERMINÍSTICO (CAE/localização/dimensão/tipo) → pesquisa SEMÂNTICA
(embeddings) → esta camada. Aqui pergunta-se a um LLM, para CADA aviso que passou, se ele é
REALMENTE adequado ao perfil do cliente. Apanha o que nem os padrões nem os embeddings
distinguem — ex: um aviso para "entidades gestoras de resíduos urbanos" que passa por omissão
(não restringe CAE/dimensão) mas não serve uma consultora de gestão.

Degradação graciosa (NUNCA esconder por acidente): sem OPENROUTER_API_KEY, em falha de
rede/API, timeout, ou resposta ilegível → devolve {} e o match mantém TODOS os avisos que a
semântica devolveu. Só filtra um aviso quando o LLM diz explicitamente que NÃO é adequado.
"""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

_URL = "https://openrouter.ai/api/v1/chat/completions"
_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_TIMEOUT = 90            # o modelo é gratuito e pode ser lento
_MAX_CHARS = 1200        # truncagem de campos longos (objetivo, ações, critérios)
_MAX_LIST_ITEMS = 20     # nº máx. de itens por lista enviada

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
    s = str(value).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _trunc_list(value):
    """Lista com itens truncados e limitada a _MAX_LIST_ITEMS."""
    if not value:
        return []
    return [_trunc(x, 400) for x in list(value)[:_MAX_LIST_ITEMS] if x]


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
        "covered_areas": [a.geographic_area for a in grant.covered_areas.all() if a.geographic_area],
    }


def _parse_verdicts(content: str) -> dict[int, dict]:
    """Extrai {grant_id: {adequate, reason}} da resposta do LLM (tolerante a lixo à volta)."""
    if not content:
        return {}
    data = _loads(content.strip())
    if data is None:  # tenta extrair o bloco JSON de dentro de texto/markdown
        for opener, closer in (("[", "]"), ("{", "}")):
            i, j = content.find(opener), content.rfind(closer)
            if i != -1 and j > i:
                data = _loads(content[i:j + 1])
                if data is not None:
                    break
    if data is None:
        return {}
    items = data.get("results") if isinstance(data, dict) else data
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


def validate_matches(client_metadata: dict, grants: list) -> dict[int, dict]:
    """{grant_id: {"adequate": bool, "reason": str}} para os avisos `grants`, via OpenRouter.

    {} (nada validado → nada filtrado) se: não houver OPENROUTER_API_KEY, a lista for vazia,
    ou a chamada/parse falhar. Uma ÚNICA chamada valida todos os avisos.
    """
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key or not grants:
        return {}

    payload = {
        "client": _client_payload(client_metadata),
        "grants": [_grant_payload(g) for g in grants],
    }
    body = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    }
    try:
        resp = requests.post(
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
        logger.warning("Validação LLM (OpenRouter) falhou (rede) — match segue sem filtro LLM: %s", exc)
        return {}

    # Corpo CRU da resposta (o que o OpenRouter devolveu) — logado SEMPRE, mesmo em erro, para
    # inspeção. É aqui que se vê um erro do OpenRouter (rate-limit, modelo indisponível…) que de
    # outra forma só aparecia como um KeyError sem contexto.
    raw = resp.text
    logger.info("Validação LLM (OpenRouter) [%s] HTTP %s — corpo cru:\n%s",
                _MODEL, resp.status_code, raw[:4000])

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "Validação LLM (OpenRouter) sem resposta utilizável (%s) — match segue sem filtro LLM.",
            type(exc).__name__)
        return {}

    verdicts = _parse_verdicts(content or "")
    if not verdicts:
        logger.warning("Validação LLM: resposta ilegível — match segue sem filtro LLM.")
    return verdicts
