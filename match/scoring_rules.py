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

from common.text import normalize as _normalize


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


def _pattern_specificity(cae: str, patterns) -> int:
    """Comprimento do PREFIXO mais específico (mais longo) entre os `patterns` que batem no
    `cae`; -1 se nenhum bater. Ex: cae '91100' vs '911**' → 3; vs '91***' → 2."""
    best = -1
    for pattern in patterns or []:
        cleaned_pattern = str(pattern).strip()
        star = cleaned_pattern.find("*")
        prefix = cleaned_pattern if star == -1 else cleaned_pattern[:star]
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
    client_caes = {str(cae_code).strip() for cae_code in (client.get("cae_codes") or []) if cae_code}
    if not client_caes:
        return False

    included = [pattern for pattern in (opportunity.get("included_caes") or []) if pattern]
    excluded = [pattern for pattern in (opportunity.get("excluded_caes") or []) if pattern]
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
    normalized_regions = [_normalize(region_name) for region_name in (opportunity.get("eligible_regions") or []) if region_name]
    if any(("todo o pais" in region_name or "nacional" in region_name or "continente" in region_name) for region_name in normalized_regions):
        return True

    # Inclui a NUTS II/III resolvida do município (nuts.json): o cliente traz o concelho
    # ("Faro") mas os avisos usam a NUTS II ("Algarve"). É correspondência EXATA (não fuzzy).
    client_tokens = [
        token for token in (
            _normalize(client.get(field))
            for field in ("region", "city", "county", "nuts_ii", "nuts_iii", "nuts_ii_old")
        ) if token
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


def _word_pattern(phrase: str) -> re.Pattern:
    """Regex da frase por PALAVRAS INTEIRAS (plural 's' opcional em cada palavra).

    Evita falsos positivos por substring — 'media' já não casa dentro de 'imediata',
    nem 'cim' dentro de 'decimo' — que ativavam indevidamente o filtro rígido e
    excluíam clientes elegíveis. Opera sobre texto normalizado (sem acentos)."""
    words = [re.escape(word) + "s?" for word in phrase.split()]
    return re.compile(r"\b" + r"\s+".join(words) + r"\b")


# "micro" também aparece colado ("microempresas") — palavra única, precisa de padrão próprio.
_MICRO_PAT = re.compile(r"\bmicro(empresas?)?\b")
_PME_PAT = _word_pattern("pme")
_PME_PHRASE_PAT = _word_pattern("pequenas e medias")
_PEQUENA_PAT = _word_pattern("pequena")
_MEDIA_PAT = _word_pattern("media")
_GRANDE_PAT = _word_pattern("grande")
_NAO_PME_PAT = _word_pattern("nao pme")

# Padrões que indicam que a dimensão é admitida (por dimensão do cliente).
_DIMENSION_MATCH_PATTERNS = {
    "micro":   (_MICRO_PAT, _PME_PAT, _PME_PHRASE_PAT),
    "pequena": (_PEQUENA_PAT, _PME_PAT, _PME_PHRASE_PAT),
    "media":   (_MEDIA_PAT, _PME_PAT, _PME_PHRASE_PAT),
    "grande":  (_GRANDE_PAT, _NAO_PME_PAT),
}


def match_dimension(client: dict, opportunity: dict) -> bool:
    """A dimensão da empresa do cliente é admitida nos critérios da oportunidade."""
    dimension = _normalize(client.get("dimension"))
    if not dimension:
        return False

    patterns = _DIMENSION_MATCH_PATTERNS.get(dimension, (_word_pattern(dimension),))
    text = opportunity.get("eligibility_text", "")
    return any(pattern.search(text) for pattern in patterns)


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

# Padrões compilados (palavra inteira) por tipo — "micro" usa o padrão próprio para
# apanhar também "microempresas"; os restantes vêm de _word_pattern(sinónimo).
_ENTITY_TYPE_PATTERNS = {
    etype: tuple(_MICRO_PAT if syn == "micro" else _word_pattern(syn) for syn in synonyms)
    for etype, synonyms in ENTITY_TYPE_SYNONYMS.items()
}


def match_entity_type(client: dict, opportunity: dict) -> bool:
    """O tipo/natureza da entidade do cliente é admitido nos critérios da oportunidade.

    Lê o `beneficiary_text` (destinatários finais + beneficiários por ação) — QUEM se pode
    candidatar — e não o `eligibility_text`, que inclui as CONDIÇÕES legais. O boilerplate
    "não ser uma empresa em dificuldade" fazia um aviso para entidades gestoras de resíduos
    parecer um aviso para empresas."""
    entity_type = _normalize(client.get("entity_type"))
    if not entity_type:
        return False
    patterns = _ENTITY_TYPE_PATTERNS.get(entity_type, (_word_pattern(entity_type),))
    text = opportunity.get("beneficiary_text", "")
    return any(pattern.search(text) for pattern in patterns)


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
    entity_type = employees if employees is not None else 0
    try:
        region_name = float(revenue) if revenue is not None else 0.0
    except (TypeError, ValueError):
        region_name = 0.0
    if entity_type < 10 and region_name <= 2000000:
        return "micro"
    if entity_type < 50 and region_name <= 10000000:
        return "pequena"
    if entity_type < 250 and region_name <= 50000000:
        return "media"
    return "grande"


# --- Elegibilidade (filtro rígido: só aparecem os elegíveis) ---------------

def grant_allowed_dimensions(eligibility_text: str) -> set[str]:
    """Dimensões admitidas pelo aviso, lidas do texto de elegibilidade (normalizado).

    Conjunto vazio ⇒ o aviso não restringe dimensão (todas elegíveis). Nota: micro,
    pequena e média SÃO PME; grande NÃO é PME.
    """
    token = eligibility_text or ""
    allowed: set[str] = set()
    if _PME_PAT.search(token) or _PME_PHRASE_PAT.search(token):
        allowed |= {"micro", "pequena", "media"}
    if _MICRO_PAT.search(token):
        allowed.add("micro")
    if _PEQUENA_PAT.search(token):
        allowed.add("pequena")
    if _MEDIA_PAT.search(token):
        allowed.add("media")
    if _GRANDE_PAT.search(token) or _NAO_PME_PAT.search(token):
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


def grant_allowed_entity_types(beneficiary_text: str) -> set[str]:
    """Tipos de beneficiário admitidos pelo aviso, lidos do texto de QUEM SE PODE CANDIDATAR
    (destinatários finais + beneficiários por ação) — nunca das condições legais.

    Conjunto vazio ⇒ o aviso não restringe o tipo (todos elegíveis). Usa os mesmos sinónimos
    do matcher — se o texto mencionar o tipo, considera-o admitido."""
    token = beneficiary_text or ""
    allowed: set[str] = set()
    for etype, patterns in _ENTITY_TYPE_PATTERNS.items():
        if any(pattern.search(token) for pattern in patterns):
            allowed.add(etype)
    return allowed


def eligible_entity_type(client: dict, opportunity: dict) -> bool:
    """OK se o aviso não restringe o tipo de beneficiário, se o tipo do cliente é admitido,
    ou se o tipo do cliente é desconhecido (não dá para provar inelegibilidade).

    Lê de `beneficiary_text` (quem se pode candidatar), não do `eligibility_text` (condições)."""
    allowed = grant_allowed_entity_types(opportunity.get("beneficiary_text", ""))
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
    if not [cae_code for cae_code in (client.get("cae_codes") or []) if cae_code]:
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
