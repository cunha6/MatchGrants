"""
Regras de pontuação do motor de matching.

`SCORING_CONFIG` é a única fonte de verdade dos critérios: cada entrada define um
peso e a função (`matcher`) que decide se uma oportunidade satisfaz o critério para
um dado cliente. Para adicionar/alterar critérios ou pesos, basta editar este
dicionário — o serviço itera sobre ele sem conhecer os critérios em concreto.

Cada `matcher` tem a assinatura `(client: dict, opportunity: dict) -> bool` e opera
sobre dicionários normalizados (não sobre o ORM), mantendo as regras desacopladas da
fonte de dados das oportunidades.
"""

import unicodedata


def _normalize(text) -> str:
    """minúsculas + sem acentos + espaços colapsados (para comparações tolerantes)."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().split())


# --- Matchers -------------------------------------------------------------

def _cae_matches_pattern(cae: str, pattern: str) -> bool:
    """True se o CAE (5 dígitos) bate no padrão wildcard hierárquico.

    O padrão tem 5 caracteres: dígitos seguidos de '*' (sufixo). A comparação é por
    prefixo até ao primeiro '*'. Ex: '64***' bate em qualquer CAE que comece por '64';
    '65124' (sem '*') exige igualdade exata. Padrões mal-formados → não batem (conservador).
    """
    pattern = str(pattern).strip()
    star = pattern.find("*")
    prefix = pattern if star == -1 else pattern[:star]
    if not prefix.isdigit():
        return False
    return str(cae).startswith(prefix)


def _matches_any_pattern(client_caes: set[str], patterns) -> bool:
    """True se algum CAE do cliente bater em algum dos padrões wildcard."""
    pats = [str(p) for p in (patterns or []) if p]
    return any(_cae_matches_pattern(cae, p) for cae in client_caes for p in pats)


def match_cae(client: dict, opportunity: dict) -> bool:
    """O CAE (principal ou secundário) do cliente é elegível na oportunidade.

    Usa os padrões wildcard hierárquicos normalizados pela extração:
    - `included_caes`: se não vazio, SÓ os CAE que batem nestes padrões são elegíveis.
    - `excluded_caes`: o CAE deixa de ser elegível se bater num destes padrões.
    - ambos vazios ⇒ sem restrição de CAE ⇒ elegível (equivale ao antigo "Todos").
    Regra final: elegível ⇔ (included vazio OU bate em included) E NÃO bate em excluded.
    """
    client_caes = {str(c).strip() for c in (client.get("cae_codes") or []) if c}
    if not client_caes:
        return False

    included = opportunity.get("included_caes") or []
    excluded = opportunity.get("excluded_caes") or []

    if _matches_any_pattern(client_caes, excluded):
        return False
    if included:
        return _matches_any_pattern(client_caes, included)
    return True


def match_location(client: dict, opportunity: dict) -> bool:
    """A localização do cliente (cidade/região/concelho) cabe nas regiões elegíveis."""
    opp_regions = [r for r in (opportunity.get("eligible_regions") or []) if r]
    if not opp_regions:
        return False

    normalized_regions = [_normalize(r) for r in opp_regions]
    if any(("todo o pais" in r or "nacional" in r or "continente" in r) for r in normalized_regions):
        return True

    client_tokens = [
        _normalize(client.get(field))
        for field in ("region", "city", "county")
        if client.get(field)
    ]
    if not client_tokens:
        return False

    for region in normalized_regions:
        for token in client_tokens:
            if token and (token in region or region in token):
                return True
    return False


def match_dimension(client: dict, opportunity: dict) -> bool:
    """A dimensão da empresa do cliente é admitida nos critérios da oportunidade."""
    dimension = _normalize(client.get("dimension"))
    if not dimension:
        return False

    # PME = micro + pequena + média
    synonyms = {
        "micro": ("micro", "microempresa", "pme"),
        "pequena": ("pequena", "pequenas empresas", "pme"),
        "media": ("media", "medias empresas", "pme"),
        "grande": ("grande", "grandes empresas", "nao pme"),
    }.get(dimension, (dimension,))

    text = opportunity.get("eligibility_text", "")
    return any(syn in text for syn in synonyms)


def match_entity_type(client: dict, opportunity: dict) -> bool:
    """O tipo/natureza da entidade do cliente é admitido nos critérios da oportunidade."""
    entity_type = _normalize(client.get("entity_type"))
    if not entity_type:
        return False

    synonyms = {
        "empresa": ("empresa", "empresas", "sociedade", "pme"),
        "associacao": ("associacao", "associacoes"),
        "municipio": ("municipio", "municipios", "autarquia", "camara municipal"),
        "ensino": ("ensino", "escola", "universidade", "instituicao de ensino"),
        "ong": ("ong", "organizacao nao governamental", "ipss", "sem fins lucrativos"),
    }.get(entity_type, (entity_type,))

    text = opportunity.get("eligibility_text", "")
    return any(syn in text for syn in synonyms)


# --- Classificação de dimensão (UE PME) -----------------------------------

def classify_dimension(employees, revenue) -> str | None:
    """Classifica a dimensão da empresa a partir do nº de trabalhadores e do volume de
    negócios anual (proveitos operacionais), segundo os limiares da UE:

        micro:   < 10 trabalhadores   e  ≤ 2 M€
        pequena: < 50 trabalhadores   e  ≤ 10 M€
        media:   < 250 trabalhadores  e  ≤ 50 M€
        grande:  ≥ 250 trabalhadores  ou  > 50 M€

    Devolve None quando nenhum dos valores é conhecido.
    """
    if employees is None and revenue is None:
        return None
    e = employees if employees is not None else 0
    try:
        r = float(revenue) if revenue is not None else 0.0
    except (TypeError, ValueError):
        r = 0.0
    if e < 10 and r <= 2_000_000:
        return "micro"
    if e < 50 and r <= 10_000_000:
        return "pequena"
    if e < 250 and r <= 50_000_000:
        return "media"
    return "grande"


# --- Elegibilidade (filtro rígido: só aparecem os elegíveis) ---------------

def grant_allowed_dimensions(eligibility_text: str) -> set[str]:
    """Dimensões admitidas pelo aviso, lidas do texto de elegibilidade (normalizado).

    Conjunto vazio ⇒ o aviso não restringe dimensão (todas elegíveis). Nota: micro,
    pequena e média SÃO PME; grande NÃO é PME.
    """
    t = eligibility_text or ""
    allowed: set[str] = set()
    if "pme" in t or "pequenas e medias" in t:
        allowed |= {"micro", "pequena", "media"}
    if "micro" in t:
        allowed.add("micro")
    if "pequena" in t:
        allowed.add("pequena")
    if "media" in t:
        allowed.add("media")
    if "grande" in t or "nao pme" in t:
        allowed.add("grande")
    return allowed


def eligible_cae(client: dict, opportunity: dict) -> bool:
    """OK se o aviso não restringe CAE; caso contrário o CAE do cliente tem de casar."""
    included = opportunity.get("included_caes") or []
    excluded = opportunity.get("excluded_caes") or []
    if not included and not excluded:
        return True
    return match_cae(client, opportunity)


def eligible_location(client: dict, opportunity: dict) -> bool:
    """OK se o aviso não lista regiões; caso contrário a localização tem de casar."""
    if not (opportunity.get("eligible_regions") or []):
        return True
    return match_location(client, opportunity)


def eligible_dimension(client: dict, opportunity: dict) -> bool:
    """OK se o aviso não restringe dimensão, se a dimensão do cliente é admitida, ou se
    a dimensão do cliente é desconhecida (não dá para provar inelegibilidade)."""
    allowed = grant_allowed_dimensions(opportunity.get("eligibility_text", ""))
    if not allowed:
        return True
    dim = _normalize(client.get("dimension"))
    if not dim:
        return True
    return dim in allowed


ELIGIBILITY = {
    "cae": {"label": "CAE", "check": eligible_cae},
    "location": {"label": "Localização", "check": eligible_location},
    "dimension": {"label": "Dimensão", "check": eligible_dimension},
}


def is_eligible(client: dict, opportunity: dict) -> tuple[bool, list[dict]]:
    """(elegível, detalhe). O cliente só é elegível se passar TODOS os critérios rígidos
    que o aviso especifica (região, CAE, dimensão)."""
    breakdown = []
    ok = True
    for key, rule in ELIGIBILITY.items():
        passed = rule["check"](client, opportunity)
        ok = ok and passed
        breakdown.append({"criterion": key, "label": rule["label"], "eligible": passed})
    return ok, breakdown


def missing_required_fields(client: dict) -> list[dict]:
    """Campos obrigatórios do cliente em falta ([{field,label}, ...]); vazia se completo.

    Indispensáveis para um match fiável: CAE e localização. Se faltarem, não dá para
    decidir a elegibilidade de avisos que os restrinjam — por isso são PEDIDOS ao
    utilizador em vez de excluir avisos em silêncio. A dimensão NÃO entra aqui: é
    tolerante (em falta ⇒ decide pelos outros critérios).
    """
    missing = []
    if not [c for c in (client.get("cae_codes") or []) if c]:
        missing.append({"field": "cae", "label": "CAE (código de atividade)"})
    if not any(client.get(f) for f in ("region", "city", "county")):
        missing.append({"field": "region", "label": "Localização / região"})
    return missing


# --- Configuração (fonte de verdade dos critérios) ------------------------

SCORING_CONFIG = {
    "cae": {
        "label": "CAE",
        "weight": 20,
        "matcher": match_cae,
    },
    "location": {
        "label": "Localização",
        "weight": 20,
        "matcher": match_location,
    },
    "dimension": {
        "label": "Dimensão",
        "weight": 20,
        "matcher": match_dimension,
    },
    "entity_type": {
        "label": "Tipo de Entidade",
        "weight": 20,
        "matcher": match_entity_type,
    },
}

# Pontuação máxima possível (soma de todos os pesos) — útil para percentagens.
MAX_SCORE = sum(rule["weight"] for rule in SCORING_CONFIG.values())
