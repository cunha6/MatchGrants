"""
Classificação de documentos de aviso e estratégia de consolidação de alterações.

Um aviso pode ter vários documentos: base, republicação consolidada, alteração (diff),
prorrogação, retificação, anexos. Esta camada decide qual usar para extração e se é
preciso consolidar diffs sobre o documento base.

Funções puras (sem I/O) — testáveis isoladamente.
"""

import re

from .IA.normalizers import normalize_text

# Tipos de documento
BASE = "base"
REPUBLICATION = "republication"
AMENDMENT = "amendment"
PRORROGATION = "prorrogation"
RECTIFICATION = "rectification"
ANNEX = "annex"
OTHER = "other"

# Linguagem típica de um diff de alteração (não repete o aviso inteiro)
_DIFF_LANGUAGE_RE = re.compile(
    r"passa a ter a seguinte redac|onde se le|onde consta|passa a constar"
    r"|e alterad[oa]|e revogad[oa]|e aditad[oa]|sao alterad|fica alterad",
    re.I,
)

# Marca de número de alteração: "3ª Alteração", "7.ª Alt", "1a Republicação", "_7.ª Alt"
# (sem \b inicial: o dígito pode vir colado a "_" ou "-", ex: "2024-14_7.ª Alt")
_AMENDMENT_ORDINAL_RE = re.compile(r"(\d+)\s*[.ºªao]*\s*(?:alt|republica)", re.I)

# Secções-template de um aviso Portugal2030 completo (presença ⇒ documento consolidado)
_TEMPLATE_SECTIONS = (
    "designacao do aviso",
    "apoio para",
    "entidades que se podem candidatar",
    "periodo de candidaturas",
    "dotacao",
    "custos elegiveis",
    "criterios de selecao",
    "obrigacoes dos beneficiarios",
)


def classify_document(name: str, pages: int = 0, nature: str | None = None) -> str:
    """Classifica um documento pelo nome (+ páginas/natureza, quando disponíveis)."""
    n = normalize_text(name)

    if nature == "convite" or "convite" in n:
        return OTHER
    # Anexos PRIMEIRO: "Anexo ...", guias, orientações técnicas, fichas de verificação.
    # (evita falsos positivos como "Anexo ... alterações climáticas" → AMENDMENT)
    if n.startswith("anexo") or n.startswith("annex") or "guia" in n or "orientac" in n or "verificac" in n:
        return ANNEX
    if "prorrogac" in n:
        return PRORROGATION
    if "retificac" in n or "rectificac" in n or "errata" in n:
        return RECTIFICATION
    if "republicac" in n or "consolidad" in n:
        return REPUBLICATION
    # Alteração: exige ordinal ("3ª Alteração"/"7.ª Alt"), "alteração ao/do aviso" ou aditamento.
    # NÃO basta "alterac" solto — evita apanhar "alterações climáticas".
    if (_AMENDMENT_ORDINAL_RE.search(n) or "alteracao ao aviso" in n
            or "alteracao do aviso" in n or "aditamento" in n):
        return AMENDMENT
    # "aviso"/"aac"/"concurso" como PALAVRA INTEIRA — evita "CustosElegíveisAviso", etc.
    if re.search(r"\b(aviso|aac|concurso)\b", n):
        return BASE
    return OTHER


def amendment_ordinal(name: str) -> int:
    """Número da alteração/republicação (ex: '3ª Alteração' → 3). 0 se não houver."""
    m = _AMENDMENT_ORDINAL_RE.search(normalize_text(name))
    return int(m.group(1)) if m else 0


def is_consolidated_markdown(markdown: str) -> bool:
    """True se o markdown contém o template de um aviso completo (>=4 secções-chave)."""
    norm = normalize_text(markdown)
    hits = sum(1 for s in _TEMPLATE_SECTIONS if s in norm)
    return hits >= 4


def has_diff_language(markdown: str) -> bool:
    """True se o texto usa linguagem de alteração pontual ('onde consta X passa a Y').

    Normaliza antes de comparar: os padrões estão sem acentos, mas o texto real tem
    ("redação", "onde se lê", "é alterado") — sem isto os diffs reais não eram detetados."""
    return bool(_DIFF_LANGUAGE_RE.search(normalize_text(markdown)))


def needs_consolidation(markdown: str) -> bool:
    """
    True quando o documento parece ser uma alteração-diff (linguagem de diff e SEM
    o template completo do aviso). Nesse caso é preciso o documento base + consolidação.
    """
    return has_diff_language(markdown) and not is_consolidated_markdown(markdown)


def order_documents(docs: list[dict]) -> list[dict]:
    """
    Ordena documentos do mais recente/consolidado para o mais antigo.
    `docs`: lista de dicts com chaves 'name' (e opcionalmente 'pages', 'nature').
    Prioriza republicações consolidadas, depois alterações por ordinal decrescente,
    depois o aviso base.
    """
    _RANK = {REPUBLICATION: 0, AMENDMENT: 1, RECTIFICATION: 2, BASE: 3,
             PRORROGATION: 4, ANNEX: 5, OTHER: 6}

    def key(doc: dict):
        doc_type = doc.get("type") or classify_document(
            doc.get("name", ""), doc.get("pages", 0), doc.get("nature")
        )
        return (_RANK.get(doc_type, 9), -amendment_ordinal(doc.get("name", "")))

    return sorted(docs, key=key)
