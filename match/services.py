"""
Serviço de integração (nif.pt) e motor de matching.

Fluxo: NIF -> consulta à API nif.pt -> validação da existência/estado do
contribuinte -> extração de metadados -> cruzamento com as oportunidades ativas
(avisos.Grant) segundo o SCORING_CONFIG -> lista de matches ordenada por score.

A view limita-se a invocar `NifMatchingService().evaluate(nif)`.

Responsabilidades vizinhas vivem em módulos próprios: o cliente do nif.pt e a
normalização dos seus dados em company_metadata.py, e o ciclo de vida do lead
(viewer) em leads.py. Aqui fica o motor de matching e a orquestração do pedido.
"""

import hashlib
import json
import logging
import threading

import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Q

from avisos.models import Grant, GrantCae, GrantEmbedding
from common.cae import cae_all_prefixes
from common.email_validation import email_error_label
from .company_metadata import extract_metadata
from .scoring_rules import (
    SCORING_CONFIG, MAX_SCORE, _normalize, is_eligible,
    missing_required_fields,
)
from . import embeddings
from . import leads
from . import grant_embeddings
from . import llm_validation
from .ranking import (
    active_phase_id, company_area_id, effective_budget_rate,
    max_financing_rate_from_rates,
)

logger = logging.getLogger(__name__)

# Rotação das chaves da API nif.pt (NIF_KEY, NIF_KEY1..N do .env): a cada consulta usa-se a
# chave seguinte, uma de cada vez (round-robin), para repartir os pedidos por várias chaves.
# O índice é global ao processo e protegido por lock (seguro entre threads do gunicorn).
_nif_key_lock = threading.Lock()
_nif_key_index = 0


def _next_nif_key() -> str | None:
    """Chave nif.pt seguinte na rotação. Lê settings.NIF_KEYS a cada chamada (respeita
    override_settings nos testes). Se não houver lista, cai para a NIF_KEY única. None se
    não houver nenhuma configurada."""
    global _nif_key_index
    keys = list(getattr(settings, "NIF_KEYS", None) or [])
    if not keys:
        return getattr(settings, "NIF_KEY", None)
    with _nif_key_lock:
        key = keys[_nif_key_index % len(keys)]
        _nif_key_index += 1
    return key


def _company_general_text(metadata: dict) -> str:
    """Texto GERAL do cliente (compara com o embedding GENERAL do aviso) — junta tudo o que
    o descreve: atividade + nome + tipo de entidade + CAE + localização.

    A `activity` (descrição do nif.pt) é o sinal semântico mais forte; os restantes campos
    acrescentam contexto (setor via CAE, afinidade regional via localização). Quando a atividade
    vem vazia, o resto garante que a semântica tem sempre com que trabalhar."""
    parts: list[str] = []
    activity = (metadata.get("activity") or "").strip()
    if activity:
        parts.append(activity)
    if metadata.get("name"):
        parts.append(str(metadata["name"]))
    if metadata.get("entity_type"):
        parts.append(str(metadata["entity_type"]))
    cae_codes = [str(cae_code) for cae_code in (metadata.get("cae_codes") or []) if cae_code]
    if cae_codes:
        parts.append("CAE: " + ", ".join(cae_codes))
    location_parts = [str(metadata.get(field_name)) for field_name in ("region", "county", "city", "address") if metadata.get(field_name)]
    if location_parts:
        parts.append("Localização: " + ", ".join(location_parts))
    return "\n".join(parts)


def _company_sector_text(metadata: dict) -> str:
    """Texto SETORIAL do cliente (compara com o embedding SECTOR do aviso): a ATIVIDADE
    PRINCIPAL — a descrição do nif.pt é o que define o domínio económico da empresa.

    Sem localização nem nome: aqui só interessa "o que a empresa faz", para casar com os
    setores-alvo do aviso. Fallback ao CAE + nome quando o nif.pt não traz atividade — senão
    a empresa ficaria sem dimensão setorial nenhuma."""
    activity = (metadata.get("activity") or "").strip()
    if activity:
        return activity
    parts: list[str] = []
    cae_codes = [str(cae_code) for cae_code in (metadata.get("cae_codes") or []) if cae_code]
    if cae_codes:
        parts.append("CAE: " + ", ".join(cae_codes))
    if metadata.get("name"):
        parts.append(str(metadata["name"]))
    return "\n".join(parts)


class NifValidationError(Exception):
    """NIF inexistente, inválido ou contribuinte inativo (→ HTTP 400)."""


class NifServiceError(Exception):
    """Falha de configuração ou de comunicação com a API externa (→ HTTP 502)."""


class MissingClientDataError(Exception):
    """Faltam dados obrigatórios para devolver o match (→ HTTP 422 `needs_more_info`).
    `fields` é a lista [{field,label}, ...] do que falta. Duas origens possíveis:
    CAE/localização em falta (não dá para decidir elegibilidade — ver
    `missing_required_fields`), ou contacto (email/nome/função) em falta em quem não tem
    sessão (retém os `matches` já calculados até o pop-up ser preenchido — ver
    `_missing_contact_fields`/`evaluate`)."""

    def __init__(self, fields: list[dict]):
        self.fields = fields
        missing_labels = ", ".join(field_name["label"] for field_name in fields) or "dados em falta"
        super().__init__(f"São necessárias mais informações para fazer o match: {missing_labels}.")


# Contacto pedido a quem não tem sessão antes de revelar os resultados (ver
# NifMatchingService.evaluate). Não vem do nif.pt — vem do pop-up preenchido pelo utilizador.
_CONTACT_FIELDS = [
    ("email", "Email"),
    ("name", "Nome"),
    ("job_title", "Função"),
]


def _missing_contact_fields(contact: dict) -> list[dict]:
    """Campos de contacto em falta ([{field,label}, ...]); vazia se completo.

    O email também é tratado como "em falta" quando está preenchido mas não serve para
    captar um lead de EMPRESA: formato inválido (ex: "x"), domínio inexistente, ou domínio
    de webmail genérico/gratuito/descartável — ver `common.email_validation.email_error_label`
    (partilhada com o registo de conta em `users/service.py`, mesma regra de negócio). Label
    ESPECÍFICA ao motivo, para o front-end conseguir mostrar por que é que o pop-up voltou a
    pedir o email."""
    missing = []
    for field, label in _CONTACT_FIELDS:
        value = contact.get(field)
        if not value:
            missing.append({"field": field, "label": label})
        elif field == "email":
            error_label = email_error_label(value)
            if error_label:
                missing.append({"field": field, "label": error_label})
    return missing


class NifMatchingService:
    """Consulta o nif.pt, valida o contribuinte e cruza-o com as oportunidades."""

    API_URL = "https://www.nif.pt/?json=1&q={nif}&key={key}"
    TIMEOUT = 15  # segundos
    # Máximo de avisos enviados à validação LLM (os N mais relevantes). Limita custo/latência
    # da chamada externa; os restantes passam sem validação LLM (ver _apply_llm_validation).
    LLM_VALIDATION_CAP = 10

    def __init__(self, api_key: str | None = None):
        # Chave explícita (ex: testes) prevalece; senão pega a próxima da rotação (uma de
        # cada vez). Como a view cria um NifMatchingService por pedido, cada consulta ao
        # nif.pt usa a chave seguinte do ciclo NIF_KEY, NIF_KEY1, NIF_KEY2, NIF_KEY3, NIF_KEY4.
        self.api_key = api_key or _next_nif_key()

    # --- Integração HTTP + validação -------------------------------------

    def fetch_company(self, nif: str) -> dict:
        """Consulta a API e devolve o registo do contribuinte já validado.

        Validação obrigatória (não basta a estrutura do NIF):
        - `nif_validation` tem de ser True no JSON devolvido;
        - o `status` da empresa tem de ser "active".
        Caso contrário levanta NifValidationError.
        """
        if not self.api_key:
            raise NifServiceError("NIF_KEY não configurada no ambiente.")

        url = self.API_URL.format(nif=nif, key=self.api_key)
        try:
            response = requests.get(url, timeout=self.TIMEOUT)  
            response.raise_for_status() 
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            # A str(exc) das exceções do requests inclui o URL — e o URL leva a NIF_KEY.
            # Não pode ir na resposta HTTP nem, já agora, no log: os logs vão para a consola
            # (docker logs) e para ficheiro, e uma chave de API não tem nada que ficar lá.
            logger.warning(
                "Falha ao contactar a API nif.pt (nif=%s): %s", nif,
                str(exc).replace(self.api_key, "***"),
            )
            raise NifServiceError(
                f"Falha ao contactar a API nif.pt ({type(exc).__name__}). Tenta novamente."
            )

        # Erro reportado pela própria API (ex: "Key not valid") → erro de serviço.
        if payload.get("result") == "error":
            raise NifServiceError(f"API nif.pt: {payload.get('message') or 'erro desconhecido'}")

        # Garantir a EXISTÊNCIA do contribuinte (não apenas validade matemática).
        if not payload.get("nif_validation"):
            raise NifValidationError("NIF inválido ou inexistente.")

        records = payload.get("records") or {}
        record = records.get(str(nif)) or next(iter(records.values()), None)
        if not record:
            raise NifValidationError("Contribuinte não encontrado para o NIF indicado.")

        if (record.get("status") or "").lower() != "active":
            raise NifValidationError("O contribuinte existe mas não está ativo.")

        return record

    # --- Motor de matching -----------------------------------------------

    def process_matches(self, client_metadata: dict,
                        company_vectors: dict[str, list[float]] | None = None) -> list[dict]:
        """Cruza os metadados do cliente com as oportunidades ativas.

        FILTRO RÍGIDO DE ELEGIBILIDADE: só entram na lista as oportunidades para as
        quais o cliente é elegível em TODOS os critérios que o aviso especifica —
        região, CAE e dimensão. Basta uma incompatibilidade (ex.: cliente do Algarve
        e aviso só para o Norte, ou CAE fora do âmbito, ou micro empresa num aviso só
        para grandes) para a oportunidade nem sequer aparecer.

        ORDENAÇÃO (o que o utilizador vê no topo):
          1) RELEVÂNCIA semântica — score combinado (0.60 setorial + 0.40 geral) entre os
             embeddings do cliente (`company_vectors`) e os do aviso; mais direcionados ao topo.
          2) TAXA de financiamento EFETIVA (maior primeiro).
          3) DOTAÇÃO EFETIVA (maior primeiro).
        A taxa/dotação efetivas são as da FASE ativa e da ÁREA da empresa (ver ranking).

        Só LÊ embeddings (pré-carregados por prefetch) — nunca os gera. Quem os gera é o save
        do aviso (db_service) e o comando `embed_grants`; um aviso ainda sem embeddings fica
        com relevância None e o ranking cai para taxa+dotação.

        CAMADA FINAL (LLM): depois de ordenar, um LLM gratuito (OpenRouter) valida cada aviso
        contra o perfil do cliente e os NÃO adequados são removidos (ver llm_validation). Sem
        OPENROUTER_API_KEY ou em falha, nada é filtrado — o match mantém a lista da semântica.
        """
        company_vectors = company_vectors or {}
        client_tokens = [
            _normalize(client_metadata.get(field_name))
            for field_name in ("region", "county", "city", "nuts_ii", "nuts_iii", "nuts_ii_old") if client_metadata.get(field_name)
        ]

        results = []
        grant_by_id = {}
        for grant in self._active_opportunities(client_metadata):
            opportunity = self._grant_to_opportunity(grant)

            eligible, eligibility = is_eligible(client_metadata, opportunity)
            if not eligible:
                continue  # não elegível → não aparece

            score = 0
            breakdown = []
            for key, rule in SCORING_CONFIG.items():
                matched = rule["matcher"](client_metadata, opportunity)
                points = rule["weight"] if matched else 0
                score += points
                breakdown.append({
                    "criterion": key,
                    "label": rule["label"],
                    "matched": matched,
                    "points": points,
                })

            # Relevância semântica: 0.60 setorial + 0.40 geral (None se sem embeddings).
            relevance, sector_sim, general_sim = grant_embeddings.relevance(
                company_vectors, grant_embeddings.grant_vectors(grant),
            )

            # Dotação/taxa efetivas p/ a fase ativa + área da empresa (ligação por FK id).
            phase_id = active_phase_id(opportunity["phases"])
            area_id = company_area_id(client_tokens, opportunity["covered_areas"])
            budget, rate = effective_budget_rate(opportunity["phase_areas"], phase_id, area_id)
            if budget is None:
                budget = opportunity["total_allocation"]
            if rate is None:
                rate = max_financing_rate_from_rates(opportunity["financing_rates"])

            results.append({
                "opportunity_id": opportunity["id"],
                "grant_code": opportunity["grant_code"],
                "title": opportunity["title"],
                "score": score,
                "max_score": MAX_SCORE,
                # Score semântico combinado. Mantém o nome `activity_relevance` (contrato da
                # API) e continua a ser o 1º critério de ordenação; as duas componentes vão
                # à parte para o front-end/diagnóstico poder explicar o porquê.
                "activity_relevance": relevance,
                "sector_similarity": sector_sim,
                "general_similarity": general_sim,
                "effective_financing_rate": rate,
                "effective_budget_allocation": budget,
                "active_phase_id": phase_id,
                "matched_area_id": area_id,
                "eligibility": eligibility,
                "breakdown": breakdown,
            })
            grant_by_id[opportunity["id"]] = grant

        # Atividade 1º, depois taxa, depois dotação — todos decrescentes.
        results.sort(
            key=lambda match_row: (
                match_row["activity_relevance"] if match_row["activity_relevance"] is not None else -1.0,
                match_row["effective_financing_rate"] if match_row["effective_financing_rate"] is not None else -1.0,
                match_row["effective_budget_allocation"] if match_row["effective_budget_allocation"] is not None else -1.0,
            ),
            reverse=True,
        )
        return self._apply_llm_validation(client_metadata, results, grant_by_id)

    @classmethod
    def _apply_llm_validation(cls, client_metadata: dict, results: list[dict],
                              grant_by_id: dict) -> list[dict]:
        """Camada FINAL: marca cada match com `llm_adequate`/`llm_reason` e REMOVE os que o LLM
        considerou não adequados. Sem chave/em falha (verdicts vazios) nada é filtrado — cada
        match fica com llm_adequate=None e permanece na lista.

        CAP: só os `LLM_VALIDATION_CAP` avisos mais relevantes (results já vem ordenado por
        relevância) são enviados ao LLM — controla custo/latência da chamada externa. Os
        restantes passam sem validação (llm_adequate=None): ficam sempre na lista, no fundo,
        onde já estavam por relevância."""
        to_validate = results[:cls.LLM_VALIDATION_CAP]
        grants_in_order = [grant_by_id[match_row["opportunity_id"]] for match_row in to_validate]
        verdicts = llm_validation.validate_matches(client_metadata, grants_in_order) \
            if grants_in_order else {}

        final = []
        for match_row in results:
            # Só os avisos do top-N enviados têm veredito; os restantes ficam com None (passam).
            verdict = verdicts.get(match_row["opportunity_id"])
            match_row["llm_adequate"] = verdict["adequate"] if verdict else None
            match_row["llm_reason"] = verdict["reason"] if verdict else None
            # Só é removido quando o LLM diz EXPLICITAMENTE que não é adequado.
            if verdict is None or verdict["adequate"]:
                final.append(match_row)
        removed = len(results) - len(final)
        if removed:
            logger.info("Validação LLM: %d de %d avisos (top-%d) removidos por não adequados.",
                        removed, min(len(results), cls.LLM_VALIDATION_CAP), cls.LLM_VALIDATION_CAP)
        return final

    # TTL do cache que guarda o match já calculado à espera do contacto (ver `evaluate`).
    CONTACT_CACHE_TTL = 900  # 15 min — tempo de sobra para preencher o pop-up
    _CONTACT_CACHE_PREFIX = "match_pending_contact:"

    @classmethod
    def _contact_cache_key(cls, nif: str, overrides: dict | None, scope: str | None) -> str:
        """Chave do match retido à espera do contacto.

        Inclui `scope` (a sessão de quem pediu) — sem isso, dois visitantes a avaliar o MESMO
        NIF partilhavam a entrada: o segundo consumia o resultado do primeiro e o primeiro
        pagava um recálculo completo. E inclui um resumo dos `overrides`, para que reenviar
        com o CAE/região CORRIGIDOS recalcule, em vez de devolver o resultado do valor antigo.
        """
        payload = json.dumps(overrides or {}, sort_keys=True, default=str)
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return f"{cls._CONTACT_CACHE_PREFIX}{scope or '-'}:{nif}:{digest}"

    @staticmethod
    def _sanitize_contact(contact: dict, missing_contact: list[dict]) -> dict:
        """Nunca grava um email inválido/descartável no perfil nem dispara o email de
        boas-vindas — é tratado como se não tivesse vindo (create_or_update_viewer não
        sabe distinguir "descartável" de "válido", por isso sanitiza-se aqui antes)."""
        if contact.get("email") and any(f["field"] == "email" for f in missing_contact):
            return {**contact, "email": None}
        return contact

    def evaluate(self, nif: str, overrides: dict | None = None,
                 create_viewer: bool = True, contact: dict | None = None,
                 cache_scope: str | None = None) -> dict:
        """Orquestra o fluxo completo e devolve o payload pronto para a resposta.

        `overrides` permite preencher dados que o nif.pt/enriquecimento não trazem
        (CAE, região, dimensão) — usado quando o utilizador responde ao pedido de mais
        informações. Se, mesmo assim, faltar um campo obrigatório (CAE ou localização),
        levanta MissingClientDataError em vez de excluir avisos em silêncio.

        `create_viewer`: só se regista a empresa como viewer quando o match vem de alguém
        NÃO autenticado (é aí que o viewer serve — guardar o lead que consultou os apoios).
        Um utilizador autenticado (admin, commercial…) que faça um match está a consultar,
        não a gerar lead: nesse caso não se cria nem se toca no perfil, `contact` é ignorado,
        e `viewer_user_id` vem a None.

        `contact` (email/nome/função) — GATE de captação de lead, só para quem não tem
        sessão. Um pedido com `contact` ainda VAZIO (a 1ª chamada, só o NIF, antes do
        pop-up aparecer) CALCULA o match já nesta chamada (a procura não espera por um
        contacto que nem chegou a ser pedido) e fica em cache (`CONTACT_CACHE_TTL`) à
        espera dele. Mas um `contact` já SUBMETIDO (o pop-up foi preenchido) e ainda assim
        INVÁLIDO nem chega a gerar essa pesquisa — a validação em si (formato +
        disposable_email_domains.json) é local, em milisegundos, e dizer isso ao fim de
        uma pesquisa inteira (nif.pt + embeddings + LLM, o essencial da latência do match)
        só porque o email veio mal só atrasava o pop-up sem propósito. Nos dois casos
        levanta-se MissingClientDataError (mesmo 422/`needs_more_info` do CAE/localização
        em falta). O lead (dados da empresa) só é registado quando a pesquisa chega a
        correr — um contacto submetido-mas-inválido não fica registado; a próxima
        tentativa (mesmo NIF, contacto vazio ou já corrigido) é que o regista.

        `cache_scope`: identificador de quem está a pedir (a chave da sessão, passada pela
        view) — isola a entrada em cache por visitante. Ver `_contact_cache_key`.
        """
        contact = contact or {}

        # Contacto SUBMETIDO (pelo menos um campo preenchido) mas INVÁLIDO: rejeita já,
        # sem tocar em nif.pt/cache/LLM — ver docstring acima. `_missing_contact_fields`
        # não depende de metadata (só de `contact`), por isso pode correr aqui, antes de
        # sequer se saber quem é a empresa.
        if create_viewer and any(contact.values()):
            early_missing = _missing_contact_fields(contact)
            if early_missing:
                raise MissingClientDataError(early_missing)

        cache_key = self._contact_cache_key(nif, overrides, cache_scope)

        if create_viewer:
            cached = cache.get(cache_key)
            if cached:
                metadata, matches = cached["metadata"], cached["matches"]
                missing_contact = _missing_contact_fields(contact)
                user = leads.create_or_update_viewer(
                    metadata, self._sanitize_contact(contact, missing_contact))
                if not missing_contact:
                    cache.delete(cache_key)
                    return {
                        "company": dict(metadata), "nif": metadata["nif"],
                        "viewer_user_id": user.id, "matches": matches,
                    }
                # Continua incompleto/inválido: mantém em cache para a tentativa seguinte
                # (não recalcula nif.pt/embeddings só porque o contacto ainda não serve).
                raise MissingClientDataError(missing_contact)
            # sem cache (expirou, ou é mesmo a 1ª chamada para este nif/overrides/scope) —
            # calcula tudo abaixo, como de costume.

        record = self.fetch_company(nif)
        metadata = extract_metadata(record)
        metadata = self._apply_overrides(metadata, overrides)

        missing_contact = _missing_contact_fields(contact) if create_viewer else []
        user = None
        if create_viewer:
            # O lead (dados da empresa) é registado ANTES de qualquer 422 — mesmo que falte o
            # CAE/localização, quem consultou fica guardado (é o objetivo do viewer).
            user = leads.create_or_update_viewer(
                metadata, self._sanitize_contact(contact, missing_contact))

        missing = missing_required_fields(metadata)
        if missing:
            raise MissingClientDataError(missing)

        # Embeddings do cliente, gerados DINAMICAMENTE (nunca guardados na BD), um por
        # dimensão — a base da procura semântica que ordena os avisos mais enquadrados no topo.
        company_vectors = self._company_vectors(metadata)
        matches = self.process_matches(metadata, company_vectors)

        if missing_contact:
            cache.set(cache_key, {"metadata": metadata, "matches": matches},
                      self.CONTACT_CACHE_TTL)
            raise MissingClientDataError(missing_contact)

        # `company` expõe os dados ricos do contribuinte (incluindo a `activity`).
        return {
            "company": dict(metadata),
            "nif": metadata["nif"],
            "viewer_user_id": user.id if user else None,
            "matches": matches,
        }

    @staticmethod
    def _company_vectors(metadata: dict) -> dict[str, list[float]]:
        """{tipo: vetor} do cliente — setorial (atividade) + geral (perfil completo).

        Uma ÚNICA chamada à OpenAI para as duas dimensões (embed_many). Tipos sem texto ou
        sem API ficam de fora e a relevância renormaliza sobre os que existirem.
        """
        texts = {
            GrantEmbedding.Type.SECTOR: _company_sector_text(metadata),
            GrantEmbedding.Type.GENERAL: _company_general_text(metadata),
        }
        types = [embedding_type for embedding_type, text in texts.items() if text.strip()]
        if not types:
            return {}
        vectors = embeddings.embed_many([texts[embedding_type] for embedding_type in types])
        return {embedding_type: vector for embedding_type, vector in zip(types, vectors) if vector is not None}

    @staticmethod
    def _apply_overrides(metadata: dict, overrides: dict | None) -> dict:
        """Preenche campos EM FALTA com os dados fornecidos pelo utilizador (não
        sobrepõe dados já obtidos do nif.pt). Aceita CAE como lista ou string
        separada por vírgulas/ponto-e-vírgula."""
        overrides = overrides or {}

        if not metadata.get("cae_codes"):
            cae_codes = overrides.get("cae_codes") or overrides.get("cae")
            if isinstance(cae_codes, str):
                cae_codes = cae_codes.replace(";", ",").split(",")
            cae_codes = [str(cae_code).strip() for cae_code in (cae_codes or []) if str(cae_code).strip()]
            if cae_codes:
                metadata["cae_codes"] = cae_codes
                metadata["main_cae"] = cae_codes[0]
                metadata["secondary_cae"] = cae_codes[1:]

        if not metadata.get("region") and overrides.get("region"):
            metadata["region"] = str(overrides["region"]).strip()

        # Dimensão é opcional, mas se o utilizador a fornecer, respeita-a.
        if not metadata.get("dimension") and overrides.get("dimension"):
            metadata["dimension"] = str(overrides["dimension"]).strip().lower()

        # entity_type é INFERIDO (heurística nome/natureza); um valor explícito do utilizador
        # PREVALECE — permite corrigir a inferência ou testar como outro tipo de beneficiário.
        if overrides.get("entity_type"):
            metadata["entity_type"] = str(overrides["entity_type"]).strip().lower()

        return metadata

    # --- Auxiliares -------------------------------------------------------

    @staticmethod
    def _active_opportunities(client_metadata: dict | None = None):
        """Oportunidades consideradas ativas: avisos já processados pela IA e AINDA a decorrer
        (active=True — os terminados, cuja closing_date já passou, são escondidos do match).

        Faz prefetch das relações usadas pelo ranking (fases, áreas, dotações por
        fase/área, taxas), dos beneficiários por ação (tipo de beneficiário) e dos
        embeddings (relevância semântica) para evitar N+1 queries por aviso.

        PREFILTRO CAE em SQL (quando o cliente tem CAE): usando a tabela normalizada GrantCae,
        o Postgres devolve só os avisos SEM lista de inclusão OU cuja inclusão bate num prefixo
        do cliente — é impossível ser CAE-elegível fora desse conjunto. Assim o Python nunca
        vê os avisos com CAE incompatível; a regra fina (prefixo mais específico ↔ exclusões)
        continua no match_cae. O resultado final é IDÊNTICO ao de filtrar tudo em Python.
        """
        qs = Grant.objects.filter(ai_processed=True, active=True).prefetch_related(
            "phases", "covered_areas", "phase_areas", "financing_rates",
            "beneficiaries_by_action", "embeddings",
        )
        cae_codes = [str(cae_code).strip() for cae_code in (client_metadata or {}).get("cae_codes", []) if cae_code]
        if cae_codes:
            prefixes = set().union(*(cae_all_prefixes(cae_code) for cae_code in cae_codes))
            included = GrantCae.objects.filter(
                grant=OuterRef("pk"), kind=GrantCae.INCLUDED)
            matching = included.filter(prefix__in=prefixes)
            qs = qs.annotate(
                _has_included=Exists(included), _matches_included=Exists(matching),
            ).filter(Q(_has_included=False) | Q(_matches_included=True))
        return qs

    @staticmethod
    def _grant_to_opportunity(grant: Grant) -> dict:
        """Converte um Grant no dicionário normalizado que os matchers e o ranking consomem."""
        eligibility_parts = list(grant.beneficiary_eligibility_criteria or [])
        eligibility_parts += list(grant.final_recipients or [])

        # QUEM se pode candidatar — fonte SEPARADA das CONDIÇÕES legais. Os
        # `beneficiary_eligibility_criteria` são requisitos ("não ser uma empresa em
        # dificuldade", "não ter processo de injunção…"), não dizem quem é o beneficiário —
        # e o boilerplate legal da UE menciona "empresa" numa cláusula de EXCLUSÃO, o que
        # levava o filtro a concluir que o aviso era para empresas. O tipo de beneficiário
        # lê-se só de onde ele está mesmo declarado: destinatários finais + beneficiários
        # por ação.
        beneficiary_parts = list(grant.final_recipients or [])
        for beneficiary in grant.beneficiaries_by_action.all():
            beneficiary_parts += list(beneficiary.entities or [])

        return {
            "id": grant.id,
            "grant_code": grant.grant_code,
            "title": grant.title,
            "included_caes": grant.included_caes or [],
            "excluded_caes": grant.excluded_caes or [],
            "eligible_regions": grant.eligible_regions or [],
            "eligibility_text": _normalize(" | ".join(str(eligibility_part) for eligibility_part in eligibility_parts)),
            "beneficiary_text": _normalize(" | ".join(str(eligibility_part) for eligibility_part in beneficiary_parts)),
            # Dados para o ranking por dotação/taxa efetivas (fase ativa × área da empresa).
            "total_allocation": grant.total_allocation,
            "phases": [
                {"id": eligibility_part.id, "start_date": eligibility_part.start_date, "end_date": eligibility_part.end_date}
                for eligibility_part in grant.phases.all()
            ],
            "covered_areas": [
                {"id": a.id, "geographic_area": a.geographic_area}
                for a in grant.covered_areas.all()
            ],
            "phase_areas": [
                {"phase_id": pa.phase_id, "area_id": pa.area_id, "fund_name": pa.fund_name,
                 "budget_allocation": pa.budget_allocation, "max_financing_rate": pa.max_financing_rate}
                for pa in grant.phase_areas.all()
            ],
            "financing_rates": [
                {"max_global_rate": fr.max_global_rate, "base_rate": fr.base_rate}
                for fr in grant.financing_rates.all()
            ],
        }
