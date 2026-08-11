"""Divide markdown em chunks semânticos: 1 chunk por secção de heading."""

import json
import logging
import re
from pathlib import Path

from .normalizers import normalize_text
from common.docling.docling_ocr import _clean_ocr

logger = logging.getLogger(__name__)

normalize = normalize_text

_CONFIG_PATH = Path(__file__).parent / "mapping_config.json"

_MD_HEADER_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*$')

# Enumeradores de subcritério de grelha: "A)", "A.1.", "B.1", "C)", "D.2."...
_CRITERION_ENUM_RE = re.compile(r'^[A-H](\.\d+)*[.)]')


def load_mapping(config_path: Path | str | None = None) -> tuple[dict, dict, dict]:
    path = Path(config_path) if config_path else _CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return (
            config["mapeamento"],
            config["categoria_para_prompts"],
            config.get("field_pt_terms", {}),
        )
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Ficheiro de configuração não encontrado: {path}\n"
            "Cria o ficheiro mapping_config.json na raiz do projeto."
        )


# FIELD_PT_TERMS: termos PT por campo (nomes de campo são EN, texto dos chunks é PT). Usado
# pelo rescue do P7 para encontrar a secção certa de um campo vazio. Vive no mapping_config.json.
MAPEAMENTO, CATEGORIA_PARA_PROMPTS, FIELD_PT_TERMS = load_mapping()


def _get_prompt_categories(prompt: str) -> frozenset[str]:
    return frozenset(cat for cat, prompts in CATEGORIA_PARA_PROMPTS.items() if prompt in prompts.split(","))

CATS_P1 = _get_prompt_categories("P1")
CATS_P2 = _get_prompt_categories("P2")
CATS_P3 = _get_prompt_categories("P3")
CATS_P4 = _get_prompt_categories("P4")
CATS_P5 = _get_prompt_categories("P5")
CATS_P6 = _get_prompt_categories("P6")

_HARD_SPLIT_CATEGORIAS = {
    "documentos_requisitos",
    "operacoes_elegibilidade",
    "despesas",
}
_HARD_SPLIT_MIN_CHARS = 800

# Formas de rótulo de tipologia/ação (lista única, partilhada pelos dois regexes abaixo).
# "Ação tipo a)" já traz o seu delimitador ')'; as outras precisam de ':'/'-' no split.
_ACAO_FORM_ACAO = r'(?:Para a )?[Aa][çc][ãa]o tipo [a-zA-Z]\)'
_ACAO_FORM_OUTRAS = r'Tipologia [a-zA-Z0-9\.\-]+|Componente [A-Z0-9]|Medida [0-9\.]+'

# Split: onde cortar — rótulo no início de linha (as "outras" exigem ':'/'-' a seguir).
_HARD_SPLIT_PATTERN = re.compile(
    rf'\n(?=(?:{_ACAO_FORM_ACAO}|(?:{_ACAO_FORM_OUTRAS})\s*[:\-]))',
    re.IGNORECASE,
)
# Extração: só o nome do rótulo dentro de um bloco já cortado (sem delimitador).
_ACAO_LABEL_PATTERN = re.compile(
    rf'({_ACAO_FORM_ACAO}|{_ACAO_FORM_OUTRAS})',
    re.IGNORECASE,
)


def _hard_split(
    text: str,
    title: str,
    grant_code: str,
    source: str,
    category: str,
    prompt_source: str,
    page_start: int,
    page_end: int,
    is_annex: bool = False,
) -> list[dict]:
    blocks = _HARD_SPLIT_PATTERN.split(text)
    if len(blocks) <= 1:
        return []

    result: list[dict] = []
    for block in blocks:
        block = block.strip()
        if len(block) < 30:
            continue

        match = _ACAO_LABEL_PATTERN.search(block)
        label = match.group(1).strip().upper() if match else "CONTEXTO GERAL"

        enriched_text = (
            f"DOCUMENTO: {grant_code}\n"
            f"SECÇÃO PAI: {title}\n"
            f"APLICAÇÃO: {label}\n"
            f"---\n"
            f"{block}"
        )

        result.append({
            "title":         f"{title} — {label}",
            "section":       title,
            "category":      category,
            "prompt_source": prompt_source,
            "grant_code":    grant_code,
            "source":        source,
            "page_start":    page_start,
            "page_end":      page_end,
            "is_annex":      is_annex,
            "text":          enriched_text,
        })

    return result


# ---------------------------------------------------------------------------
# _get_annex_category — REESCRITA COMPLETA
# Problema original: mapeamento por letra do Anexo era frágil e errava:
#   - Anexo A-3 (RIS3) -> criterios_indicadores (devia ser ignorar)
#   - Anexo A-4 (Grelha) -> documentos_requisitos (devia ser criterios_indicadores)
#   - Anexo B (Legislação) -> criterios_indicadores (devia ser legislacao)
#   - Anexo B-3 (OCS) -> criterios_indicadores (devia ser despesas)
#   - Anexo C (Legislação) -> despesas (devia ser legislacao)
# Solução: match por CONTEÚDO DO TÍTULO (palavras-chave semânticas)
#          e só usa letra do Anexo como último recurso
# ---------------------------------------------------------------------------

# Regras semânticas por conteúdo do título — ordem: mais específica primeiro.
# IMPORTANTE: regras de CONTEÚDO têm prioridade sobre "ignorar". Um subcritério como
# "A.1 Alinhamento às prioridades da RIS3" (dentro de "Anexo A-3 Critérios de seleção")
# tem de ir para criterios_indicadores, não ser descartado por conter "RIS3".
_ANEXO_CONTENT_RULES: list[tuple[re.Pattern, str]] = [
    # INSTRUMENTOS TERRITORIAIS — mapas, delimitações geográficas ITI/DLBC/PDUI
    (re.compile(r'territ[oó]rio\s+iti|delimitac|mapa\s+territ|pdui|dlbc|cim\b|amp\b|nuts\s+iii', re.I), "instrumentos_territoriais"),
    # CRITÉRIOS — grelha de avaliação/seleção/mérito
    (re.compile(r'criterios?\s+de\s+selec|grelha|referencial\s+de\s+merito|avaliacao\s+do\s+merito|merito\s+do\s+projeto|metodologia\s+de\s+aval|criterios?\s+de\s+aval|pondera|subcriter', re.I), "criterios_indicadores"),
    # DESPESAS — OCS, custos simplificados
    (re.compile(r'custos?\s+simplificados?|ocs\b|pagamento\s+dos\s+apoios|metodologia\s+de\s+custo', re.I), "despesas"),
    # LEGISLAÇÃO — diplomas, regulamentos, legislação aplicável
    (re.compile(r'legislac|regulamentac|diplomas?|legislacao\s+aplic|regulamentos?\s+aplic|normativos?\s+aplic', re.I), "legislacao"),
    # DOCUMENTOS DNSH / alterações climáticas
    (re.compile(r'nao\s+prejudicar|dnsh|alteracoes\s+climaticas|tagging\s+climatico', re.I), "documentos_requisitos"),
    # DOCUMENTOS DE CANDIDATURA — lista de documentos a submeter
    (re.compile(r'documentos?\s+necessarios|documentos?\s+a\s+apresentar|instrucao\s+da\s+candidatura', re.I), "documentos_requisitos"),
    # ORIENTAÇÕES DE GESTÃO / receitas / outros documentos operacionais
    (re.compile(r'orientac|receitas|projetos?\s+geradores', re.I), "operacoes_elegibilidade"),
]

# Só descartado quando NENHUMA regra de conteúdo bate — um anexo puramente de estratégia.
_ANEXO_IGNORE_RULE = re.compile(
    r'ris\s*3|ris3|estrategia\s+de\s+especializacao|smart\s+specialisation', re.I
)

# ---------------------------------------------------------------------------
# map_category — CORRIGIDA
# Problema original: iteração em ordem de inserção do dict, com keywords
# genéricas (ex: "enquadramento", "norma", "calendário") aparecendo em
# categorias de índice menor e ganhando sobre keywords específicas em
# categorias de índice maior.
# Solução: match pelo comprimento da keyword (mais longa = mais específica).
# ---------------------------------------------------------------------------
def map_category(title: str, content: str = "") -> str:
    """
    Classifica um chunk pela keyword mais longa (mais específica) que faça match.
    Prioridade: anexo (regras semânticas) → título → "ignorar" → conteúdo (fallback).
    """
    annex_cat = _get_annex_category(title)
    if annex_cat:
        return annex_cat

    title_n = normalize(title)

    # 1. Título (sinal forte)
    cat = _best_keyword_match(title_n)
    if cat:
        return cat

    # 2. Ignorar — só quando é o único sinal do título (índice, preâmbulo, sumário...)
    if any(normalize(kw) and normalize(kw) in title_n for kw in MAPEAMENTO.get("ignorar", [])):
        return "ignorar"

    # 3. Conteúdo (fallback fraco — só quando o título nada diz)
    if content:
        cat = _best_keyword_match(normalize(content[:400]))
        if cat:
            return cat

    return "outros"

def _get_annex_category(header: str) -> str | None:
    """
    Classifica anexos pelo conteúdo semântico do título (caminho completo de headings).
    Regras de conteúdo têm prioridade; "ignorar" só quando é o único sinal.
    Retorna None se nada fizer match (vai para o mapeamento geral).
    """
    h_norm = normalize(header.strip())

    if not (h_norm.startswith("anexo") or h_norm.startswith("annex")):
        return None

    for pattern, cat in _ANEXO_CONTENT_RULES:
        if pattern.search(h_norm):
            return cat

    # Conteúdo não bateu — só agora consideramos descartar (anexo puro de estratégia)
    if _ANEXO_IGNORE_RULE.search(h_norm):
        return "ignorar"

    return None


def _best_keyword_match(data: str) -> str | None:
    """
    Categoria cuja keyword mais longa (mais específica) aparece em `data`.
    Exclui "ignorar" (uma keyword curta de ignore nunca rouba conteúdo real).
    """
    best_cat, best_len = None, 0
    for category, keywords in MAPEAMENTO.items():
        if category == "ignorar":
            continue
        for kw in keywords:
            kw_n = normalize(kw)
            if kw_n and kw_n in data and len(kw_n) > best_len:
                best_cat, best_len = category, len(kw_n)
    return best_cat


def _parse_markdown_sections(md_text: str) -> list[tuple[tuple[str, ...], str]]:
    """
    Divide o markdown em secções preservando a ancestralidade dos headings.
    Devolve [(caminho_de_headings, texto), ...] pela ordem do documento.
    Cada heading `#`..`######` empilha/desempilha por nível, tal como a hierarquia
    que o HierarchicalChunker do docling produzia — mas a partir do markdown puro.
    """
    sections: list[tuple[tuple[str, ...], str]] = []
    stack: list[tuple[int, str]] = []
    current_key: tuple[str, ...] = ("Preâmbulo",)
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            sections.append((current_key, text))
        buf.clear()

    for line in md_text.splitlines():
        m = _MD_HEADER_RE.match(line.strip())
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_key = tuple(section_text for _, section_text in stack) or (title,)
        else:
            buf.append(line)
    flush()
    return sections


def chunk_by_markdown(md_text: str, grant_code: str, source: str) -> list[dict]:
    """
    Cria chunks semânticos a partir de markdown (1 chunk por secção de heading).
    Independente do docling — testável e reutilizável para consolidação de documentos.
    """
    md_text = _clean_ocr(md_text)
    chunks: list[dict] = []
    active_category = "outros"

    for key, text in _parse_markdown_sections(md_text):
        # Caminho completo de headings (ancestralidade) para classificação;
        # garante que sub-chunks herdam o contexto do pai (ex: "Anexo A-3 Critérios > A.1").
        full_path = " > ".join(key)
        title = " > ".join(key[-2:]) if len(key) >= 2 else key[-1]
        is_annex = "anexo" in normalize_text(full_path) or "annex" in normalize_text(full_path)

        detected_category = map_category(full_path, text)

        # Subcritérios da grelha (A.1, A.2, B.1, C), D.2...) surgem como headers irmãos
        # no markdown e perdem a ancestralidade "Critérios". Dentro da grelha, o
        # enumerador de critério força criterios_indicadores (override de keyword genérica).
        if active_category == "criterios_indicadores" and _CRITERION_ENUM_RE.match(key[-1].strip()):
            detected_category = "criterios_indicadores"

        if detected_category == "ignorar":
            continue

        if detected_category != "outros":
            active_category = detected_category

        if len(text) < 30:
            continue

        primary_cat = detected_category if detected_category != "outros" else active_category

        if detected_category == "outros":
            logger.debug("[herdado -> %s]: %r", primary_cat, title[:50])

        prompt_source = CATEGORIA_PARA_PROMPTS.get(primary_cat, "")

        if primary_cat in _HARD_SPLIT_CATEGORIAS and len(text) >= _HARD_SPLIT_MIN_CHARS:
            sub = _hard_split(
                text=text, title=title, grant_code=grant_code, source=source,
                category=primary_cat, prompt_source=prompt_source,
                page_start=0, page_end=0, is_annex=is_annex,
            )
            if sub:
                for sc in sub:
                    sc["chunk_index"] = len(chunks)
                    chunks.append(sc)
                continue

        chunks.append({
            "title":         title,
            "section":       title,
            "category":      primary_cat,
            "prompt_source": prompt_source,
            "grant_code":    grant_code,
            "source":        source,
            "chunk_index":   len(chunks),
            "page_start":    0,
            "page_end":      0,
            "is_annex":      is_annex,
            "text":          text,
        })

    return chunks
