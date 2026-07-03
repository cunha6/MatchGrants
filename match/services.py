"""
Serviço de integração (nif.pt) e motor de matching.

Fluxo: NIF -> consulta à API nif.pt -> validação da existência/estado do
contribuinte -> extração de metadados -> cruzamento com as oportunidades ativas
(avisos.Grant) segundo o SCORING_CONFIG -> lista de matches ordenada por score.

A view limita-se a invocar `NifMatchingService().evaluate(nif)`.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction

from avisos.models import Grant
from users.models import UserProfile
from .models import NifCompany
from .scoring_rules import (
    SCORING_CONFIG, MAX_SCORE, _normalize, classify_dimension, is_eligible,
    missing_required_fields,
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

        # Região: SEMPRE do SQLite (fonte autoritativa); só cai para o nif.pt se o
        # enriquecimento não a tiver. Concelho/distrito seguem a mesma preferência.
        region = (enrich.region if enrich else "") or geo.get("region") or ""
        county = (enrich.municipality if enrich else "") or geo.get("county") or ""
        district = (enrich.district if enrich else "") or ""

        pc4, pc3 = record.get("pc4"), record.get("pc3")
        return {
            "nif": nif,
            "name": record.get("title") or "",
            "nature": nature,
            "nature_label": NATURE_LABELS.get(nature, nature),
            "entity_type": NATURE_TO_ENTITY_TYPE.get(nature),
            "dimension": dimension,
            "employees": employees,
            "operating_revenue": float(operating_revenue) if operating_revenue is not None else None,
            "cae_codes": cae_codes,
            "main_cae": cae_codes[0] if cae_codes else None,
            "secondary_cae": cae_codes[1:],
            "address": record.get("address") or "",
            "postal_code": f"{pc4}-{pc3}" if pc4 and pc3 else (pc4 or None),
            "city": record.get("city") or geo.get("county") or "",
            "region": region,
            "county": county,
            "district": district,
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

    def process_matches(self, client_metadata: dict) -> list[dict]:
        """Cruza os metadados do cliente com as oportunidades ativas.

        FILTRO RÍGIDO DE ELEGIBILIDADE: só entram na lista as oportunidades para as
        quais o cliente é elegível em TODOS os critérios que o aviso especifica —
        região, CAE e dimensão. Basta uma incompatibilidade (ex.: cliente do Algarve
        e aviso só para o Norte, ou CAE fora do âmbito, ou micro empresa num aviso só
        para grandes) para a oportunidade nem sequer aparecer.

        Para as elegíveis calcula ainda o score (SCORING_CONFIG) e devolve a lista
        ordenada do maior para o menor, com o detalhe da elegibilidade e do score.
        """
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

            results.append({
                "opportunity_id": opportunity["id"],
                "grant_code": opportunity["grant_code"],
                "title": opportunity["title"],
                "score": score,
                "max_score": MAX_SCORE,
                "eligibility": eligibility,
                "breakdown": breakdown,
            })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results

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

        # `company` expõe os dados ricos do contribuinte; `matches` vem ordenado.
        company = {k: v for k, v in metadata.items() if k != "activity"}
        return {
            "company": company,
            "nif": metadata["nif"],
            "viewer_user_id": user.id,
            "matches": self.process_matches(metadata),
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

        return metadata

    # --- Auxiliares -------------------------------------------------------

    @staticmethod
    def _active_opportunities():
        """Oportunidades consideradas ativas: avisos já processados pela IA."""
        return Grant.objects.filter(ai_processed=True)

    @staticmethod
    def _grant_to_opportunity(grant: Grant) -> dict:
        """Converte um Grant no dicionário normalizado que os matchers consomem."""
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
        }
