"""
Serviço de integração (nif.pt) e motor de matching.

Fluxo: NIF -> consulta à API nif.pt -> validação da existência/estado do
contribuinte -> extração de metadados -> cruzamento com as oportunidades ativas
(avisos.Grant) segundo o SCORING_CONFIG -> lista de matches ordenada por score.

A view limita-se a invocar `NifMatchingService().evaluate(nif)`.
"""

import logging
import re
import threading

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from avisos.models import Grant, GrantCae, GrantEmbedding
from common.cae import cae_all_prefixes
from users.models import UserProfile
from .disposable_email import is_disposable_email
from .models import NifCompany
from .nuts import nuts_for
from .ctt import ctt_lookup
from .scoring_rules import (
    SCORING_CONFIG, MAX_SCORE, _normalize, classify_dimension, is_eligible,
    missing_required_fields,
)
from . import embeddings
from . import grant_embeddings
from . import llm_validation
from . import notifications
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


def promote_viewer_to_client(nif: str) -> dict | None:
    """Promove um viewer a client: muda o role e ativa a conta (is_active=True).

    Idempotente. Devolve os dados atualizados ou None se não existir perfil com o NIF.
    Nota: as credenciais (username/password) são definidas mais tarde, num passo próprio
    — aqui a conta fica ativa mas ainda sem password utilizável até esse passo.
    """
    profile = UserProfile.objects.filter(nif=nif).select_related("user").first()
    if profile is None:
        return None

    user = profile.user
    if profile.role != UserProfile.CLIENT:
        profile.role = UserProfile.CLIENT
        profile.save(update_fields=["role"])
    if not user.is_active:
        user.is_active = True
        user.save(update_fields=["is_active"])

    return {
        "user_id": user.id,
        "nif": nif,
        "role": profile.role,
        "is_active": user.is_active,
        "has_login": user.has_usable_password(),
    }


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
    caes = [str(c) for c in (metadata.get("cae_codes") or []) if c]
    if caes:
        parts.append("CAE: " + ", ".join(caes))
    loc = [str(metadata.get(f)) for f in ("region", "county", "city", "address") if metadata.get(f)]
    if loc:
        parts.append("Localização: " + ", ".join(loc))
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
    caes = [str(c) for c in (metadata.get("cae_codes") or []) if c]
    if caes:
        parts.append("CAE: " + ", ".join(caes))
    if metadata.get("name"):
        parts.append(str(metadata["name"]))
    return "\n".join(parts)


# Código de natureza jurídica (structure.nature do nif.pt) → descrição legível.
NATURE_LABELS = {
    "UNI": "Sociedade Unipessoal por Quotas",
    "LDA": "Sociedade por Quotas",
    "SA": "Sociedade Anónima",
    "COO": "Cooperativa",
    "ASS": "Associação",
    "FUN": "Fundação",
    "ACE": "Agrupamento Complementar de Empresas",
    "AEIE": "Agrupamento Europeu de Interesse Económico",
    "SUC": "Sucursal",
    "MUT": "Mutualidade / Associação Mutualista",
    "EIRL": "Estabelecimento Individual de Responsabilidade Limitada",
    "ENI": "Empresário em Nome Individual",
}

# Código de natureza → tipo de entidade usado no matching (ver match_entity_type).
NATURE_TO_ENTITY_TYPE = {
    "UNI": "empresa", "LDA": "empresa", "SA": "empresa", "EIRL": "empresa",
    "ENI": "empresa", "ACE": "empresa", "AEIE": "empresa", "SUC": "empresa",
    "COO": "cooperativa",
    "ASS": "associacao", "MUT": "associacao",
    "FUN": "fundacao",
}

# Padrões no NOME → tipo de beneficiário. Servem para as entidades públicas/sociais que a
# natureza jurídica do nif.pt NÃO distingue (município, junta, misericórdia...). Ordem: do
# mais específico para o mais genérico (o primeiro que casar ganha).
_NAME_TYPE_PATTERNS = [
    (re.compile(r"comunidade intermunicipal|\bcim\b|entidade intermunicipal", re.I), "intermunicipio"),
    (re.compile(r"[aá]rea metropolitana|multimunicipal", re.I), "multimunicipio"),
    (re.compile(r"\b(junta|uni[aã]o)\s+de\s+freguesias?\b", re.I), "junta_freguesia"),
    (re.compile(r"miseric[oó]rdia|santa casa", re.I), "misericordia"),
    (re.compile(r"munic[ií]pio|c[aâ]mara municipal", re.I), "municipio"),
    (re.compile(r"funda[cç][aã]o", re.I), "fundacao"),
    (re.compile(r"cooperativa|\bcrl\b", re.I), "cooperativa"),
    (re.compile(r"universidade|instituto polit[eé]cnico|agrupamento de escolas|\bescola\b", re.I), "ensino"),
    (re.compile(r"associa[cç][aã]o", re.I), "associacao"),
]


def infer_entity_type(name: str | None, nature: str | None) -> str | None:
    """Infere o tipo de beneficiário pelo NOME (entidades públicas/sociais que a natureza
    jurídica não distingue) e, em fallback, pela natureza jurídica do nif.pt.

    O nome tem prioridade porque um "Município de X" ou "Santa Casa da Misericórdia de Y" não
    tem uma natureza jurídica societária que os identifique. None quando nada permite inferir."""
    for pattern, etype in _NAME_TYPE_PATTERNS:
        if pattern.search(name or ""):
            return etype
    return NATURE_TO_ENTITY_TYPE.get(nature)


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
        labels = ", ".join(f["label"] for f in fields) or "dados em falta"
        super().__init__(f"São necessárias mais informações para fazer o match: {labels}.")


# Contacto pedido a quem não tem sessão antes de revelar os resultados (ver
# NifMatchingService.evaluate). Não vem do nif.pt — vem do pop-up preenchido pelo utilizador.
_CONTACT_FIELDS = [
    ("email", "Email"),
    ("name", "Nome"),
    ("job_title", "Função"),
]


def _missing_contact_fields(contact: dict) -> list[dict]:
    """Campos de contacto em falta ([{field,label}, ...]); vazia se completo.

    O email também é tratado como "em falta" se o domínio for webmail genérico/gratuito
    (gmail, outlook, hotmail...) ou de um serviço de email temporário/descartável (ver
    match.disposable_email) — mesmo preenchido, não serve para captar um lead de EMPRESA.
    Label distinta ("Email inválido") para o front-end conseguir mostrar por que é que o
    pop-up voltou a pedir o email."""
    missing = []
    for field, label in _CONTACT_FIELDS:
        value = contact.get(field)
        if not value:
            missing.append({"field": field, "label": label})
        elif field == "email" and is_disposable_email(value):
            missing.append({"field": field, "label": "Email inválido"})
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
            # O detalhe fica no log — a str(exc) inclui o URL com a NIF_KEY e não
            # pode ir na resposta HTTP.
            logger.warning("Falha ao contactar a API nif.pt (nif=%s): %s", nif, exc)
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

    def extract_metadata(self, record: dict) -> dict:
        """Normaliza o registo da API nos metadados usados pelo matching e no perfil.

        - `cae` vem como LISTA na API (ex: ["88102", "85100"]) — também tolera string.
        - `entity_type` é derivado de `structure.nature` (COO, ASS, SA…).
        - Captura morada, contactos, capital social e data de início de atividade.
        - `dimension` (micro/pequena/media/grande) não vem do nif.pt: é calculada a
          partir do enriquecimento por NIF (empregados + proveitos operacionais) do
          dictionary_by_nif.json, carregado na BD 'nif' (ver `_enrichment_for`). Fica
          None quando não há dados suficientes.
        """
        geo = record.get("geo") or {}
        structure = record.get("structure") or {}
        contacts = record.get("contacts") or {}
        nature = structure.get("nature")

        cae_raw = record.get("cae")
        if isinstance(cae_raw, list):
            cae_codes = [str(c) for c in cae_raw if c]
        elif cae_raw:
            cae_codes = [str(cae_raw)]
        else:
            cae_codes = []

        nif = str(record.get("nif") or "")
        enrich = self._enrichment_for(nif)
        employees = enrich.employees if enrich else None
        operating_revenue = enrich.operating_revenue if enrich else None
        # Dimensão vem pré-calculada e gravada no SQLite (load_nif_dictionary); só
        # recalcula em fallback se, por algum motivo, não estiver gravada.
        dimension = (enrich.dimension if enrich else None) \
            or classify_dimension(employees, operating_revenue)

        pc4, pc3 = record.get("pc4"), record.get("pc3")
        postal_code = f"{pc4}-{pc3}" if pc4 and pc3 else (pc4 or None)

        # CTT: o código postal (do nif.pt) → localidade/concelho/distrito precisos. Preferimos
        # estes valores; se a CTT não responder, caímos para o enriquecimento (SQLite)/nif.pt.
        ctt_local, ctt_concelho, ctt_distrito = ctt_lookup(postal_code)
        region = (enrich.region if enrich else "") or geo.get("region") or ""
        city = ctt_local or record.get("city") or geo.get("county") or ""
        county = ctt_concelho or (enrich.municipality if enrich else "") or geo.get("county") or ""
        district = ctt_distrito or (enrich.district if enrich else "") or ""

        # NUTS II/III pela CIDADE vinda da CTT (procura no nuts.json; fallback pelo concelho).
        # Assim "4800-937" → CTT dá "Guimarães" → nuts.json dá NUTS II "Norte", NUTS III "Ave".
        # `nuts_ii_old` = NUTS II standard (5 continentais) para casar com os avisos.
        nuts_ii, nuts_iii, nuts_ii_old = nuts_for(city, county)
        return {
            "nif": nif,
            "name": record.get("title") or "",
            "nature": nature,
            "nature_label": NATURE_LABELS.get(nature, nature),
            # Tipo de beneficiário: nome (entidades públicas/sociais) + natureza jurídica.
            "entity_type": infer_entity_type(record.get("title"), nature),
            "dimension": dimension,
            "employees": employees,
            "operating_revenue": float(operating_revenue) if operating_revenue is not None else None,
            "cae_codes": cae_codes,
            "main_cae": cae_codes[0] if cae_codes else None,
            "secondary_cae": cae_codes[1:],
            "address": record.get("address") or "",
            "postal_code": postal_code,
            "city": city,
            "region": region,
            "county": county,
            "district": district,
            "nuts_ii": nuts_ii,
            "nuts_iii": nuts_iii,
            "nuts_ii_old": nuts_ii_old,
            "parish": geo.get("parish") or "",
            "capital": structure.get("capital"),
            "capital_currency": structure.get("capital_currency"),
            "start_date": record.get("start_date"),
            "activity": record.get("activity") or "",
            "contacts": {
                "email": contacts.get("email"),
                "phone": contacts.get("phone"),
                "website": contacts.get("website"),
                "fax": contacts.get("fax"),
            },
        }

    @staticmethod
    def _enrichment_for(nif: str) -> NifCompany | None:
        """Registo de enriquecimento (BD 'nif') para o NIF, ou None.

        Tolerante: se a BD 'nif' ainda não existir/estiver por carregar, devolve None
        em vez de rebentar o fluxo do match.
        """
        if not nif:
            return None
        try:
            return NifCompany.objects.filter(nif=nif).first()
        except Exception:
            return None

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
            _normalize(client_metadata.get(f))
            for f in ("region", "county", "city", "nuts_ii", "nuts_iii", "nuts_ii_old") if client_metadata.get(f)
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
            key=lambda r: (
                r["activity_relevance"] if r["activity_relevance"] is not None else -1.0,
                r["effective_financing_rate"] if r["effective_financing_rate"] is not None else -1.0,
                r["effective_budget_allocation"] if r["effective_budget_allocation"] is not None else -1.0,
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
        grants_in_order = [grant_by_id[r["opportunity_id"]] for r in to_validate]
        verdicts = llm_validation.validate_matches(client_metadata, grants_in_order) \
            if grants_in_order else {}

        final = []
        for r in results:
            # Só os avisos do top-N enviados têm veredito; os restantes ficam com None (passam).
            verdict = verdicts.get(r["opportunity_id"])
            r["llm_adequate"] = verdict["adequate"] if verdict else None
            r["llm_reason"] = verdict["reason"] if verdict else None
            # Só é removido quando o LLM diz EXPLICITAMENTE que não é adequado.
            if verdict is None or verdict["adequate"]:
                final.append(r)
        removed = len(results) - len(final)
        if removed:
            logger.info("Validação LLM: %d de %d avisos (top-%d) removidos por não adequados.",
                        removed, min(len(results), cls.LLM_VALIDATION_CAP), cls.LLM_VALIDATION_CAP)
        return final

    @transaction.atomic
    def create_or_update_viewer(self, metadata: dict, contact: dict | None = None) -> User:
        """Cria/atualiza um utilizador role=viewer com os dados MÍNIMOS do lead.

        Idempotente pelo NIF: se já existir um perfil com o mesmo NIF, atualiza-o em
        vez de duplicar. O utilizador fica com username=NIF e password inutilizável
        (sem login até um admin definir credenciais).

        Só grava dados DIRETOS do nif.pt (público) — NIF, CAE, natureza jurídica, atividade,
        morada, NUTS, dimensão. Deliberadamente NÃO grava: tipo de entidade (é INFERIDO por
        nós a partir do nome+natureza, não um campo do nif.pt), capital social, faturação/nº
        empregados, nem telefone/site/fax — quem faz o match sem login não deixa esses dados
        na BD. (Um utilizador AUTENTICADO nunca chega a chamar este método — ver `evaluate`.)

        `contact` (email/nome/função): NÃO vem do nif.pt — vem do pop-up de contacto que o
        `evaluate` pede antes de revelar os resultados a quem não tem sessão. Só entra quando
        preenchido (`evaluate` só chama isto com contacto completo ou vazio, nunca parcial).
        """
        nif = metadata["nif"]

        profile = UserProfile.objects.filter(nif=nif).select_related("user").first()
        if profile:
            user, is_new = profile.user, False
        else:
            # Viewer nasce inativo e sem password — é só um registo de acesso ao match.
            # get_or_create tolera um User pré-existente com username=NIF (ex: perfil ainda
            # sem nif) — sem isto a unicidade do username rebentava com IntegrityError.
            user, is_new = User.objects.get_or_create(
                username=nif, defaults={"is_active": False}
            )
            if is_new:
                user.set_unusable_password()
                user.save()
            # O signal post_save cria o perfil (role=client); get_or_create cobre contas
            # antigas que possam não o ter.
            profile, _ = UserProfile.objects.get_or_create(user=user)

        main_cae = metadata.get("main_cae")
        profile.nif = nif
        # Dimensão do SQLite -> entity_size do perfil (mesmas choices). `or` preserva um
        # valor manual já existente quando a dimensão é desconhecida.
        profile.entity_size = metadata.get("dimension") or profile.entity_size
        profile.activity = metadata.get("activity") or profile.activity
        # CAE e natureza jurídica: campos DIRETOS do nif.pt (record["cae"] / structure.nature),
        # não inferidos por nós — por isso entram, ao contrário de entity_type (esse é
        # calculado por infer_entity_type a partir do nome + desta mesma natureza).
        if main_cae and len(main_cae) == 5:
            profile.main_cae = main_cae
        profile.secondary_cae = [c for c in (metadata.get("secondary_cae") or []) if len(str(c)) == 5]
        profile.nature = metadata.get("nature")
        # Morada.
        profile.address = metadata.get("address") or profile.address
        profile.city = metadata.get("city")
        profile.county = metadata.get("county")
        profile.parish = metadata.get("parish")
        profile.postal_code = metadata.get("postal_code")
        # NUTS: a região é reaproveitada para guardar a NUTS II RESOLVIDA (nuts.json, a mesma
        # que os avisos usam), não o texto bruto do nif.pt — cai para este só se a resolução
        # de NUTS falhar (concelho não encontrado em nuts.json).
        profile.region = (
            metadata.get("nuts_ii_old") or metadata.get("nuts_ii") or metadata.get("region")
            or profile.region
        )

        # Role/estado: só aplica viewer+inativo a contas novas ou que ainda sejam viewer.
        # Um viewer promovido a client mantém o role e o is_active — uma nova avaliação
        # do NIF atualiza os dados mas NUNCA o rebaixa.
        if is_new or profile.role == UserProfile.VIEWER:
            profile.role = UserProfile.VIEWER
            if user.is_active:
                user.is_active = False
                user.save(update_fields=["is_active"])

        # Contacto (email/nome/função) — só o que vier preenchido, nunca apaga um valor já
        # guardado com um campo vazio de um pedido anterior.
        contact = contact or {}
        had_email = bool(user.email)
        user_fields = []
        if contact.get("email"):
            user.email = str(contact["email"]).strip()
            user_fields.append("email")
        if contact.get("name"):
            user.first_name = str(contact["name"]).strip()
            user_fields.append("first_name")
        if user_fields:
            user.save(update_fields=user_fields)
        if contact.get("job_title"):
            profile.job_title = str(contact["job_title"]).strip()

        profile.save()

        # Boas-vindas: só na 1ª vez que este viewer preenche o email (não reenviar em cada
        # match seguinte do mesmo NIF). Nunca rebenta o fluxo — ver notifications.
        if not had_email and user.email:
            notifications.send_welcome_email(user.email)

        return user

    # TTL do cache que guarda o match já calculado à espera do contacto (ver `evaluate`).
    CONTACT_CACHE_TTL = 900  # 15 min — tempo de sobra para preencher o pop-up
    _CONTACT_CACHE_PREFIX = "match_pending_contact:"

    def evaluate(self, nif: str, overrides: dict | None = None,
                 create_viewer: bool = True, contact: dict | None = None) -> dict:
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
        sessão: o match é sempre CALCULADO já nesta chamada (a procura nunca espera pelo
        contacto), mas se `contact` vier incompleto, os `matches` ficam retidos — levanta
        MissingClientDataError (mesmo 422/`needs_more_info` do CAE/localização em falta) em
        vez de os devolver. O cálculo fica em cache (`CONTACT_CACHE_TTL`) para o pedido
        seguinte, já com o contacto preenchido, não repetir nif.pt/embeddings/LLM. O lead
        (dados da empresa) é sempre registado, com ou sem contacto ainda.
        """
        contact = contact or {}
        cache_key = f"{self._CONTACT_CACHE_PREFIX}{nif}"

        if create_viewer and not _missing_contact_fields(contact):
            cached = cache.get(cache_key)
            if cached:
                cache.delete(cache_key)
                metadata, matches = cached["metadata"], cached["matches"]
                user = self.create_or_update_viewer(metadata, contact)
                return {
                    "company": dict(metadata), "nif": metadata["nif"],
                    "viewer_user_id": user.id, "matches": matches,
                }
            # sem cache (expirou, ou é a 1ª chamada já com o contacto preenchido de raiz) —
            # recalcula tudo abaixo, como de costume.

        record = self.fetch_company(nif)
        metadata = self.extract_metadata(record)
        metadata = self._apply_overrides(metadata, overrides)

        missing = missing_required_fields(metadata)
        if missing:
            raise MissingClientDataError(missing)

        # Embeddings do cliente, gerados DINAMICAMENTE (nunca guardados na BD), um por
        # dimensão — a base da procura semântica que ordena os avisos mais enquadrados no topo.
        company_vectors = self._company_vectors(metadata)
        matches = self.process_matches(metadata, company_vectors)

        user = None
        if create_viewer:
            missing_contact = _missing_contact_fields(contact)
            # Um email de domínio descartável NUNCA é gravado no perfil nem dispara o email de
            # boas-vindas — é tratado como se não tivesse vindo (create_or_update_viewer não
            # sabe distinguir "descartável" de "válido", por isso sanitiza-se aqui antes).
            safe_contact = contact
            if contact.get("email") and any(f["field"] == "email" for f in missing_contact):
                safe_contact = {**contact, "email": None}
            # Regista/atualiza sempre o lead (dados da empresa) — o contacto entra se vier.
            user = self.create_or_update_viewer(metadata, safe_contact)
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
        types = [t for t, text in texts.items() if text.strip()]
        if not types:
            return {}
        vectors = embeddings.embed_many([texts[t] for t in types])
        return {t: v for t, v in zip(types, vectors) if v is not None}

    @staticmethod
    def _apply_overrides(metadata: dict, overrides: dict | None) -> dict:
        """Preenche campos EM FALTA com os dados fornecidos pelo utilizador (não
        sobrepõe dados já obtidos do nif.pt). Aceita CAE como lista ou string
        separada por vírgulas/ponto-e-vírgula."""
        overrides = overrides or {}

        if not metadata.get("cae_codes"):
            caes = overrides.get("cae_codes") or overrides.get("cae")
            if isinstance(caes, str):
                caes = caes.replace(";", ",").split(",")
            caes = [str(c).strip() for c in (caes or []) if str(c).strip()]
            if caes:
                metadata["cae_codes"] = caes
                metadata["main_cae"] = caes[0]
                metadata["secondary_cae"] = caes[1:]

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
        caes = [str(c).strip() for c in (client_metadata or {}).get("cae_codes", []) if c]
        if caes:
            prefixes = set().union(*(cae_all_prefixes(c) for c in caes))
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
        for b in grant.beneficiaries_by_action.all():
            beneficiary_parts += list(b.entities or [])

        return {
            "id": grant.id,
            "grant_code": grant.grant_code,
            "title": grant.title,
            "included_caes": grant.included_caes or [],
            "excluded_caes": grant.excluded_caes or [],
            "eligible_regions": grant.eligible_regions or [],
            "eligibility_text": _normalize(" | ".join(str(p) for p in eligibility_parts)),
            "beneficiary_text": _normalize(" | ".join(str(p) for p in beneficiary_parts)),
            # Dados para o ranking por dotação/taxa efetivas (fase ativa × área da empresa).
            "total_allocation": grant.total_allocation,
            "phases": [
                {"id": p.id, "start_date": p.start_date, "end_date": p.end_date}
                for p in grant.phases.all()
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
