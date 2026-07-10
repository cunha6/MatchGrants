"""
Serviço de integração (nif.pt) e motor de matching.

Fluxo: NIF -> consulta à API nif.pt -> validação da existência/estado do
contribuinte -> extração de metadados -> cruzamento com as oportunidades ativas
(avisos.Grant) segundo o SCORING_CONFIG -> lista de matches ordenada por score.

A view limita-se a invocar `NifMatchingService().evaluate(nif)`.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction

from avisos.models import Grant
from users.models import UserProfile
from .models import NifCompany
from .nuts import nuts_for
from .ctt import ctt_lookup
from .scoring_rules import (
    SCORING_CONFIG, MAX_SCORE, _normalize, classify_dimension, is_eligible,
    missing_required_fields,
)
from . import embeddings
from .ranking import (
    active_phase_id, company_area_id, effective_budget_rate,
    max_financing_rate_from_rates,
)


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


def _to_decimal(value):
    """Converte o capital social (ex: '0.00') em Decimal; None se não der."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_date(value):
    """Converte a data de início de atividade (ISO) em date; None se ausente/inválida."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _company_query_text(metadata: dict) -> str:
    """Texto RICO do cliente para a pesquisa semântica — junta tudo o que o descreve:
    atividade + nome + tipo de entidade + CAE + localização (região/concelho/cidade/morada).

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
    """Faltam dados obrigatórios do cliente (CAE/localização) para decidir a
    elegibilidade. Em vez de excluir avisos em silêncio, o fluxo pede esses dados
    (→ HTTP 422). `fields` é a lista [{field,label}, ...] do que falta."""

    def __init__(self, fields: list[dict]):
        self.fields = fields
        labels = ", ".join(f["label"] for f in fields) or "dados em falta"
        super().__init__(f"São necessárias mais informações para fazer o match: {labels}.")


class NifMatchingService:
    """Consulta o nif.pt, valida o contribuinte e cruza-o com as oportunidades."""

    API_URL = "https://www.nif.pt/?json=1&q={nif}&key={key}"
    TIMEOUT = 15  # segundos

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or getattr(settings, "NIF_KEY", None)

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
            raise NifServiceError(f"Falha ao contactar a API nif.pt: {exc}")

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

    def process_matches(self, client_metadata: dict, activity_vec: list[float] | None = None) -> list[dict]:
        """Cruza os metadados do cliente com as oportunidades ativas.

        FILTRO RÍGIDO DE ELEGIBILIDADE: só entram na lista as oportunidades para as
        quais o cliente é elegível em TODOS os critérios que o aviso especifica —
        região, CAE e dimensão. Basta uma incompatibilidade (ex.: cliente do Algarve
        e aviso só para o Norte, ou CAE fora do âmbito, ou micro empresa num aviso só
        para grandes) para a oportunidade nem sequer aparecer.

        ORDENAÇÃO (o que o utilizador vê no topo):
          1) RELEVÂNCIA da atividade — procura semântica (embeddings) entre a atividade
             da empresa (`activity_vec`) e o texto do aviso; mais direcionados ao topo.
          2) TAXA de financiamento EFETIVA (maior primeiro).
          3) DOTAÇÃO EFETIVA (maior primeiro).
        A taxa/dotação efetivas são as da FASE ativa e da ÁREA da empresa (ver ranking).
        """
        client_tokens = [
            _normalize(client_metadata.get(f))
            for f in ("region", "county", "city", "nuts_ii", "nuts_iii", "nuts_ii_old") if client_metadata.get(f)
        ]

        results = []
        for grant in self._active_opportunities():
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

            # Relevância semântica à atividade da empresa (None se sem embeddings).
            relevance = self._grant_relevance(grant, activity_vec)

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
                "activity_relevance": relevance,
                "effective_financing_rate": rate,
                "effective_budget_allocation": budget,
                "active_phase_id": phase_id,
                "matched_area_id": area_id,
                "eligibility": eligibility,
                "breakdown": breakdown,
            })

        # Atividade 1º, depois taxa, depois dotação — todos decrescentes.
        results.sort(
            key=lambda r: (
                r["activity_relevance"] if r["activity_relevance"] is not None else -1.0,
                r["effective_financing_rate"] if r["effective_financing_rate"] is not None else -1.0,
                r["effective_budget_allocation"] if r["effective_budget_allocation"] is not None else -1.0,
            ),
            reverse=True,
        )
        return results

    @staticmethod
    def _grant_relevance(grant: Grant, activity_vec: list[float] | None) -> float | None:
        """Similaridade semântica entre a atividade do cliente e o texto do aviso (0..1),
        ou None se não houver embeddings (sem API/atividade). Usa/atualiza a cache no Grant."""
        if activity_vec is None:
            return None
        grant_vec = NifMatchingService._grant_embedding(grant)
        if grant_vec is None:
            return None
        return embeddings.cosine(activity_vec, grant_vec)

    @staticmethod
    def _grant_embedding(grant: Grant) -> list[float] | None:
        """Embedding do aviso, da cache (Grant.activity_embedding) quando o texto não mudou;
        senão calcula-o e persiste (fallback à la carte, caso o aviso ainda não tenha sido
        embebido ao gravar). None se não houver API/erro."""
        return embeddings.ensure_grant_embedding(grant)

    @transaction.atomic
    def create_or_update_viewer(self, metadata: dict) -> User:
        """Cria/atualiza um utilizador role=viewer com os dados do nif.pt.

        Idempotente pelo NIF: se já existir um perfil com o mesmo NIF, atualiza-o em
        vez de duplicar. O utilizador fica com username=NIF e password inutilizável
        (sem login até um admin definir credenciais).
        """
        nif = metadata["nif"]
        contacts = metadata.get("contacts") or {}

        profile = UserProfile.objects.filter(nif=nif).select_related("user").first()
        if profile:
            user, is_new = profile.user, False
        else:
            # Viewer nasce inativo e sem password — é só um registo de acesso ao match.
            user = User.objects.create(username=nif, is_active=False)
            user.set_unusable_password()
            user.save()
            profile = user.profile  # criado pelo signal post_save (role=client por defeito)
            is_new = True

        email = contacts.get("email")
        if email and not user.email:
            user.email = email
            user.save(update_fields=["email"])

        main_cae = metadata.get("main_cae")
        profile.nif = nif
        profile.entity_type = metadata.get("entity_type")
        # Dimensão do SQLite -> entity_size do perfil (mesmas choices). `or` preserva um
        # valor manual já existente quando a dimensão é desconhecida.
        profile.entity_size = metadata.get("dimension") or profile.entity_size
        if main_cae and len(main_cae) == 5:
            profile.main_cae = main_cae
        profile.secondary_cae = [c for c in (metadata.get("secondary_cae") or []) if len(str(c)) == 5]
        profile.address = metadata.get("address") or profile.address
        profile.region = metadata.get("region") or profile.region
        profile.nature = metadata.get("nature")
        profile.activity = metadata.get("activity") or profile.activity
        profile.capital = _to_decimal(metadata.get("capital"))
        profile.capital_currency = metadata.get("capital_currency")
        profile.phone = contacts.get("phone")
        profile.website = contacts.get("website")
        profile.fax = contacts.get("fax")
        profile.city = metadata.get("city")
        profile.county = metadata.get("county")
        profile.parish = metadata.get("parish")
        profile.postal_code = metadata.get("postal_code")
        incorporation = _to_date(metadata.get("start_date"))
        if incorporation:
            profile.incorporation_date = incorporation

        # Role/estado: só aplica viewer+inativo a contas novas ou que ainda sejam viewer.
        # Um viewer promovido a client mantém o role e o is_active — uma nova avaliação
        # do NIF atualiza os dados mas NUNCA o rebaixa.
        if is_new or profile.role == UserProfile.VIEWER:
            profile.role = UserProfile.VIEWER
            if user.is_active:
                user.is_active = False
                user.save(update_fields=["is_active"])

        profile.save()
        return user

    def evaluate(self, nif: str, overrides: dict | None = None) -> dict:
        """Orquestra o fluxo completo e devolve o payload pronto para a resposta.

        `overrides` permite preencher dados que o nif.pt/enriquecimento não trazem
        (CAE, região, dimensão) — usado quando o utilizador responde ao pedido de mais
        informações. Se, mesmo assim, faltar um campo obrigatório (CAE ou localização),
        levanta MissingClientDataError em vez de excluir avisos em silêncio.
        """
        record = self.fetch_company(nif)
        metadata = self.extract_metadata(record)
        metadata = self._apply_overrides(metadata, overrides)
        # Regista/atualiza a empresa como viewer (já com os dados fornecidos, se houver).
        user = self.create_or_update_viewer(metadata)

        missing = missing_required_fields(metadata)
        if missing:
            raise MissingClientDataError(missing)

        # Embedding do perfil do cliente (1 chamada) — atividade + CAE + localização + tipo.
        # É a base da procura semântica que ordena os avisos mais enquadrados no topo.
        activity_vec = embeddings.embed(_company_query_text(metadata))

        # `company` expõe os dados ricos do contribuinte (incluindo a `activity`); `matches` ordenado.
        company = dict(metadata)
        return {
            "company": company,
            "nif": metadata["nif"],
            "viewer_user_id": user.id,
            "matches": self.process_matches(metadata, activity_vec),
        }

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
    def _active_opportunities():
        """Oportunidades consideradas ativas: avisos já processados pela IA e AINDA a decorrer
        (active=True — os terminados, cuja closing_date já passou, são escondidos do match).

        Faz prefetch das relações usadas pelo ranking (fases, áreas, dotações por
        fase/área, taxas) para evitar N+1 queries por aviso.
        """
        return Grant.objects.filter(ai_processed=True, active=True).prefetch_related(
            "phases", "covered_areas", "phase_areas", "financing_rates",
        )

    @staticmethod
    def _grant_to_opportunity(grant: Grant) -> dict:
        """Converte um Grant no dicionário normalizado que os matchers e o ranking consomem."""
        eligibility_parts = list(grant.beneficiary_eligibility_criteria or [])
        eligibility_parts += list(grant.final_recipients or [])
        return {
            "id": grant.id,
            "grant_code": grant.grant_code,
            "title": grant.title,
            "included_caes": grant.included_caes or [],
            "excluded_caes": grant.excluded_caes or [],
            "eligible_regions": grant.eligible_regions or [],
            "eligibility_text": _normalize(" | ".join(str(p) for p in eligibility_parts)),
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
