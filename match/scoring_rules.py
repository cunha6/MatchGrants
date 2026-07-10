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

import re
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


def _pattern_specificity(cae: str, patterns) -> int:
    """Comprimento do PREFIXO mais específico (mais longo) entre os `patterns` que batem no
    `cae`; -1 se nenhum bater. Ex: cae '91100' vs '911**' → 3; vs '91***' → 2."""
    best = -1
    for p in patterns or []:
        s = str(p).strip()
        star = s.find("*")
        prefix = s if star == -1 else s[:star]
        if prefix.isdigit() and str(cae).startswith(prefix):
            best = max(best, len(prefix))
    return best


def match_cae(client: dict, opportunity: dict) -> bool:
    """O CAE (principal ou secundário) do cliente é elegível na oportunidade.

    Padrões wildcard hierárquicos + regra do PREFIXO MAIS ESPECÍFICO GANHA (permite
    exceções em qualquer sentido):
    - "Divisão 91 elegível, EXCETO Grupo 911": included=['91***'], excluded=['911**'].
      Um CAE '91100' bate em ambos, mas '911**' (mais específico) é excluído → NÃO elegível;
      '91200' só bate no incluído → elegível.
    - "Divisão 91 EXCLUÍDA, exceto Grupo 911": excluded=['91***'], included=['911**'].
      '91100' → o incluído '911**' é mais específico → elegível; '91200' → só excluído → não.
    - ambos vazios ⇒ sem restrição ⇒ elegível.
    Elegível ⇔ existe um CAE do cliente cuja regra mais específica é uma inclusão (ou que não
    bate em nenhuma exclusão, quando não há lista positiva).
    """
    client_caes = {str(c).strip() for c in (client.get("cae_codes") or []) if c}
    if not client_caes:
        return False

    included = [p for p in (opportunity.get("included_caes") or []) if p]
    excluded = [p for p in (opportunity.get("excluded_caes") or []) if p]
    if not included and not excluded:
        return True

    for cae in client_caes:
        inc = _pattern_specificity(cae, included)   # -1 se não bater em nenhum incluído
        exc = _pattern_specificity(cae, excluded)   # -1 se não bater em nenhum excluído
        if exc == -1:
            # Não é excluído: elegível se não há lista positiva, ou se bate nela.
            if not included or inc != -1:
                return True
        elif inc > exc:
            # É excluído, mas uma inclusão MAIS ESPECÍFICA reintegra-o (exceção).
            return True
    return False


def match_location(client: dict, opportunity: dict) -> bool:
    """A localização do cliente cabe nas regiões elegíveis, OU o aviso menciona a localização
    (região/concelho/NUTS II/NUTS III) no seu texto de elegibilidade."""
    normalized_regions = [_normalize(r) for r in (opportunity.get("eligible_regions") or []) if r]
    if any(("todo o pais" in r or "nacional" in r or "continente" in r) for r in normalized_regions):
        return True

    # Inclui a NUTS II/III resolvida do município (nuts.json): o cliente traz o concelho
    # ("Faro") mas os avisos usam a NUTS II ("Algarve"). É correspondência EXATA (não fuzzy).
    client_tokens = [
        t for t in (
            _normalize(client.get(field))
            for field in ("region", "city", "county", "nuts_ii", "nuts_iii", "nuts_ii_old")
        ) if t
    ]
    if not client_tokens:
        return False

    # 1) casa com uma das regiões elegíveis (contains bidirecional).
    for region in normalized_regions:
        for token in client_tokens:
            if token in region or region in token:
                return True

    # 2) ou o aviso FALA da localização no texto de elegibilidade (palavra inteira, para não
    #    apanhar "ave" dentro de "aveiro"). Cobre avisos que dão a região só no texto.
    text = _normalize(opportunity.get("eligibility_text"))
    return any(re.search(r"\b" + re.escape(token) + r"\b", text) for token in client_tokens)


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


# Sinónimos por tipo de beneficiário (texto normalizado, sem acentos). Fonte única usada
# tanto pelo matcher (score) como pela elegibilidade (filtro).
ENTITY_TYPE_SYNONYMS = {
    "empresa":        ("empresa", "empresas", "sociedade", "pme", "micro", "pequena", "media", "grande"),
    "associacao":     ("associacao", "associacoes"),
    "cooperativa":    ("cooperativa", "cooperativas"),
    "fundacao":       ("fundacao", "fundacoes"),
    "municipio":      ("municipio", "municipios", "autarquia", "autarquias", "camara municipal", "administracao local"),
    "intermunicipio": ("comunidade intermunicipal", "comunidades intermunicipais", "intermunicipal", "cim", "entidade intermunicipal"),
    "multimunicipio": ("area metropolitana", "areas metropolitanas", "multimunicipal", "entidade metropolitana"),
    "junta_freguesia": ("junta de freguesia", "juntas de freguesia", "uniao de freguesias", "freguesia", "freguesias"),
    "misericordia":   ("misericordia", "misericordias", "santa casa"),
    "ensino":         ("ensino", "escola", "universidade", "politecnico", "instituicao de ensino"),
    "ong":            ("ong", "organizacao nao governamental", "ipss", "sem fins lucrativos"),
}


def match_entity_type(client: dict, opportunity: dict) -> bool:
    """O tipo/natureza da entidade do cliente é admitido nos critérios da oportunidade."""
    entity_type = _normalize(client.get("entity_type"))
    if not entity_type:
        return False
    synonyms = ENTITY_TYPE_SYNONYMS.get(entity_type, (entity_type,))
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


def grant_allowed_entity_types(eligibility_text: str) -> set[str]:
    """Tipos de beneficiário admitidos pelo aviso, lidos do texto de elegibilidade.

    Conjunto vazio ⇒ o aviso não restringe o tipo (todos elegíveis). Usa os mesmos sinónimos
    do matcher — se o texto mencionar o tipo, considera-o admitido."""
    t = eligibility_text or ""
    allowed: set[str] = set()
    for etype, synonyms in ENTITY_TYPE_SYNONYMS.items():
        if any(syn in t for syn in synonyms):
            allowed.add(etype)
    return allowed


def eligible_entity_type(client: dict, opportunity: dict) -> bool:
    """OK se o aviso não restringe o tipo de beneficiário, se o tipo do cliente é admitido,
    ou se o tipo do cliente é desconhecido (não dá para provar inelegibilidade)."""
    allowed = grant_allowed_entity_types(opportunity.get("eligibility_text", ""))
    if not allowed:
        return True
    etype = _normalize(client.get("entity_type"))
    if not etype:
        return True
    return etype in allowed


ELIGIBILITY = {
    "cae": {"label": "CAE", "check": eligible_cae},
    "location": {"label": "Localização", "check": eligible_location},
    "dimension": {"label": "Dimensão", "check": eligible_dimension},
    "entity_type": {"label": "Tipo de Beneficiário", "check": eligible_entity_type},
}


def is_eligible(client: dict, opportunity: dict) -> tuple[bool, list[dict]]:
    """(elegível, detalhe). O cliente só é elegível se passar TODOS os critérios rígidos
    que o aviso especifica (região, CAE, dimensão, tipo de beneficiário)."""
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
