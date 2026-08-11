"""Normalização do registo do nif.pt nos metadados que o matching e o perfil consomem.

Separado do motor de matching (match/services.py) por ser uma responsabilidade distinta:
traduzir a resposta de uma API externa (mais o enriquecimento local por NIF e a resolução
geográfica CTT→NUTS) num dicionário estável. É transformação pura sobre dados já obtidos —
não decide elegibilidade nem persiste nada — e é o único sítio a conhecer o FORMATO do
nif.pt, pelo que uma mudança nessa API só se sente aqui.
"""

import re

from common.ctt import ctt_lookup
from common.nuts import nuts_for

from .models import NifCompany
from .scoring_rules import classify_dimension


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


def _enrichment_for(nif: str) -> NifCompany | None:
    """Registo de enriquecimento (BD 'nif') para o NIF, ou None.

    Tolerante: se a BD 'nif' ainda não existir/estiver por carregar, devolve None em vez de
    rebentar o fluxo do match.
    """
    if not nif:
        return None
    try:
        return NifCompany.objects.filter(nif=nif).first()
    except Exception:
        return None


def _cae_codes(record: dict) -> list[str]:
    """CAEs do registo. Vem como LISTA na API (ex: ["88102", "85100"]); tolera string."""
    raw = record.get("cae")
    if isinstance(raw, list):
        return [str(c) for c in raw if c]
    return [str(raw)] if raw else []


def _postal_code(record: dict) -> str | None:
    """Código postal completo "1234-567" a partir de pc4/pc3; só pc4 se faltar o pc3."""
    pc4, pc3 = record.get("pc4"), record.get("pc3")
    if pc4 and pc3:
        return f"{pc4}-{pc3}"
    return pc4 or None


def _location(record: dict, enrich: NifCompany | None, postal_code: str | None) -> dict:
    """Localização resolvida: cidade/concelho/distrito + NUTS II/III.

    A CTT (a partir do código postal do nif.pt) dá a localidade/concelho/distrito precisos e
    é a fonte preferida; sem resposta, cai para o enriquecimento (SQLite) e depois para o
    nif.pt. As NUTS resolvem-se pela CIDADE vinda da CTT (fallback pelo concelho): "4800-937"
    → CTT "Guimarães" → NUTS II "Norte", NUTS III "Ave". `nuts_ii_old` é a NUTS II standard
    (as 5 continentais), a que casa com o âmbito declarado nos avisos.
    """
    geo = record.get("geo") or {}
    ctt_local, ctt_concelho, ctt_distrito = ctt_lookup(postal_code)

    city = ctt_local or record.get("city") or geo.get("county") or ""
    county = ctt_concelho or (enrich.municipality if enrich else "") or geo.get("county") or ""
    nuts_ii, nuts_iii, nuts_ii_old = nuts_for(city, county)
    return {
        "city": city,
        "county": county,
        "district": ctt_distrito or (enrich.district if enrich else "") or "",
        "region": (enrich.region if enrich else "") or geo.get("region") or "",
        "parish": geo.get("parish") or "",
        "nuts_ii": nuts_ii,
        "nuts_iii": nuts_iii,
        "nuts_ii_old": nuts_ii_old,
    }


def _size(enrich: NifCompany | None) -> dict:
    """Dimensão da empresa (micro/pequena/media/grande) + os dados que a sustentam.

    NÃO vem do nif.pt: sai do enriquecimento por NIF (dictionary_by_nif.json carregado na BD
    'nif'), onde já está pré-calculada pelo load_nif_dictionary. Só recalcula em fallback, a
    partir de empregados + proveitos operacionais. Fica None sem dados suficientes.
    """
    employees = enrich.employees if enrich else None
    revenue = enrich.operating_revenue if enrich else None
    return {
        "dimension": (enrich.dimension if enrich else None)
                     or classify_dimension(employees, revenue),
        "employees": employees,
        "operating_revenue": float(revenue) if revenue is not None else None,
    }


def extract_metadata(record: dict) -> dict:
    """Normaliza o registo da API nif.pt nos metadados usados pelo matching e no perfil.

    Junta três fontes: o próprio registo do nif.pt, o enriquecimento local por NIF
    (dimensão/empregados/proveitos) e a resolução geográfica CTT→NUTS.
    """
    structure = record.get("structure") or {}
    contacts = record.get("contacts") or {}
    nature = structure.get("nature")

    nif = str(record.get("nif") or "")
    enrich = _enrichment_for(nif)
    postal_code = _postal_code(record)

    size = _size(enrich)
    location = _location(record, enrich, postal_code)
    cae_codes = _cae_codes(record)

    return {
        "nif": nif,
        "name": record.get("title") or "",
        "nature": nature,
        "nature_label": NATURE_LABELS.get(nature, nature),
        # Tipo de beneficiário: nome (entidades públicas/sociais) + natureza jurídica.
        "entity_type": infer_entity_type(record.get("title"), nature),
        **size,
        "cae_codes": cae_codes,
        "main_cae": cae_codes[0] if cae_codes else None,
        "secondary_cae": cae_codes[1:],
        "address": record.get("address") or "",
        "postal_code": postal_code,
        **location,
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
