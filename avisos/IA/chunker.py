"""Divide o DoclingDocument em chunks semânticos: 1 chunk por secção, via HierarchicalChunker."""

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from docling.chunking import HierarchicalChunker
from .normalizers import normalize_text
from ..Docling.docling_ocr import _clean_ocr

normalize = normalize_text

_CONFIG_PATH = Path(__file__).parent / "mapping_config.json"


def load_mapping(config_path: Path | str | None = None) -> tuple[dict, dict]:
    path = Path(config_path) if config_path else _CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config["mapeamento"], config["categoria_para_prompts"]
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Ficheiro de configuração não encontrado: {path}\n"
            "Cria o ficheiro mapping_config.json na raiz do projeto."
        )


MAPEAMENTO, CATEGORIA_PARA_PROMPTS = load_mapping()


def _cats(prompt: str) -> frozenset[str]:
    return frozenset(cat for cat, prompts in CATEGORIA_PARA_PROMPTS.items() if prompt in prompts.split(","))

CATS_P1 = _cats("P1")
CATS_P2 = _cats("P2")
CATS_P3 = _cats("P3")
CATS_P4 = _cats("P4")
CATS_P5 = _cats("P5")
CATS_P6 = _cats("P6")

_chunker = HierarchicalChunker()

_HARD_SPLIT_CATEGORIAS = {
    "documentos_requisitos",
    "operacoes_elegibilidade",
    "despesas",
}
_HARD_SPLIT_MIN_CHARS = 800

_HARD_SPLIT_PATTERN = re.compile(
    r'\n(?=(?:'
    r'(?:Para a )?[Aa][çc][ãa]o tipo [a-zA-Z]\)'
    r'|Tipologia [a-zA-Z0-9\.\-]+\s*[\:\-]'
    r'|Componente [A-Z0-9]\s*[\:\-]'
    r'|Medida [0-9\.]+\s*[\:\-]'
    r'))',
    re.IGNORECASE,
)
_ACAO_LABEL_PATTERN = re.compile(
    r'((?:Para a )?[Aa][çc][ãa]o tipo [a-zA-Z]\)'
    r'|Tipologia [a-zA-Z0-9\.\-]+'
    r'|Componente [A-Z0-9]'
    r'|Medida [0-9\.]+)',
    re.IGNORECASE,
)


def _hard_split(
    texto: str,
    titulo: str,
    codigo_aviso: str,
    fonte: str,
    categoria: str,
    prompt_origem: str,
    pagina_inicio: int,
    pagina_fim: int,
) -> list[dict]:
    blocos = _HARD_SPLIT_PATTERN.split(texto)
    if len(blocos) <= 1:
        return []

    resultado: list[dict] = []
    for bloco in blocos:
        bloco = bloco.strip()
        if len(bloco) < 30:
            continue

        match = _ACAO_LABEL_PATTERN.search(bloco)
        label = match.group(1).strip().upper() if match else "CONTEXTO GERAL"

        texto_enriquecido = (
            f"DOCUMENTO: {codigo_aviso}\n"
            f"SECÇÃO PAI: {titulo}\n"
            f"APLICAÇÃO: {label}\n"
            f"---\n"
            f"{bloco}"
        )

        resultado.append({
            "titulo":        f"{titulo} — {label}",
            "secao":         titulo,
            "categoria":     categoria,
            "prompt_origem": prompt_origem,
            "codigo_aviso":  codigo_aviso,
            "fonte":         fonte,
            "pagina_inicio": pagina_inicio,
            "pagina_fim":    pagina_fim,
            "is_anexo":      "anexo" in normalize_text(titulo),
            "texto":         texto_enriquecido,
        })

    return resultado


# ---------------------------------------------------------------------------
# _get_anexo_categoria — REESCRITA COMPLETA
# Problema original: mapeamento por letra do Anexo era frágil e errava:
#   - Anexo A-3 (RIS3) -> criterios_indicadores (devia ser ignorar)
#   - Anexo A-4 (Grelha) -> documentos_requisitos (devia ser criterios_indicadores)
#   - Anexo B (Legislação) -> criterios_indicadores (devia ser legislacao)
#   - Anexo B-3 (OCS) -> criterios_indicadores (devia ser despesas)
#   - Anexo C (Legislação) -> despesas (devia ser legislacao)
# Solução: match por CONTEÚDO DO TÍTULO (palavras-chave semânticas)
#          e só usa letra do Anexo como último recurso
# ---------------------------------------------------------------------------

# Regras semânticas por conteúdo do título — ordem: mais específica primeiro
_ANEXO_CONTENT_RULES: list[tuple[re.Pattern, str]] = [
    # IGNORAR — estratégia/enquadramento sem valor de extração
    (re.compile(r'ris\s*3|ris3|estrategia\s+de\s+especializacao|smart\s+specialisation', re.I), "ignorar"),
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


def _get_anexo_categoria(header: str) -> str | None:
    """
    Classifica anexos EXCLUSIVAMENTE pelo conteúdo semântico do título.
    Não usa a letra do Anexo como critério — isso é frágil e varia por aviso.
    Retorna None se nenhuma keyword semântica fizer match (vai para mapeamento geral).
    """
    h_raw = header.strip()
    h_norm = normalize(h_raw)

    if not (h_norm.startswith("anexo") or h_norm.startswith("annex")):
        return None

    for pattern, categoria in _ANEXO_CONTENT_RULES:
        if pattern.search(h_norm):
            return categoria

    # Sem match semântico: retorna None e deixa o mapeamento geral decidir
    # (melhor do que assumir uma categoria errada por letra)
    return None


# ---------------------------------------------------------------------------
# mapear_categoria — CORRIGIDA
# Problema original: iteração em ordem de inserção do dict, com keywords
# genéricas (ex: "enquadramento", "norma", "calendário") aparecendo em
# categorias de índice menor e ganhando sobre keywords específicas em
# categorias de índice maior.
# Solução: match pelo comprimento da keyword (mais longa = mais específica).
# ---------------------------------------------------------------------------

def mapear_categoria(titulo: str, conteudo: str = "") -> str:
    """
    Classifica um chunk pela keyword mais longa (mais específica) que faça match,
    em vez da primeira que fizer match por ordem de inserção no dict.
    Resolve conflitos como:
      - "enquadramento" (obj_enquadramento) vs "enquadramento em instrumentos" (instrumentos_territoriais)
      - "norma" (legislacao) vs "normas técnicas" (operacoes_elegibilidade)
      - "calendário" (financiamento_dotacao) vs "calendário de candidaturas" (processo_decisao)
    """
    anexo_cat = _get_anexo_categoria(titulo)
    if anexo_cat:
        return anexo_cat

    titulo_n = normalize(titulo)

    # Recolher TODOS os matches com o comprimento da keyword
    matches: list[tuple[int, str]] = []  # (len_keyword, categoria)
    for categoria, palavras in MAPEAMENTO.items():
        for p in palavras:
            p_n = normalize(p)
            if p_n and p_n in titulo_n:
                matches.append((len(p_n), categoria))

    if matches:
        # Vence a keyword mais longa (mais específica)
        matches.sort(key=lambda x: x[0], reverse=True)
        return matches[0][1]

    # Fallback: conteúdo dos primeiros 400 chars — só frases multi-palavra
    if conteudo:
        conteudo_n = normalize(conteudo[:400])
        content_matches: list[tuple[int, str]] = []
        for categoria, palavras in MAPEAMENTO.items():
            if categoria == "ignorar":
                continue
            for p in palavras:
                p_n = normalize(p)
                if p_n and p_n in conteudo_n:
                    content_matches.append((len(p_n), categoria))
        if content_matches:
            content_matches.sort(key=lambda x: x[0], reverse=True)
            return content_matches[0][1]

    return "outros"


def _pages_from_doc_items(doc_items: list[Any]) -> tuple[int, int]:
    pages: list[int] = []
    for item in doc_items:
        for prov in (getattr(item, "prov", None) or []):
            p = getattr(prov, "page_no", None)
            if p:
                pages.append(p)
    if not pages:
        return (0, 0)
    return (min(pages), max(pages))


def chunk_por_headers(
    doc: Any,
    codigo_aviso: str,
    fonte: str,
) -> list[dict]:
    """
    Usa HierarchicalChunker para criar 1 chunk por secção do DoclingDocument.
    """
    groups: dict[tuple, list] = {}

    for raw in _chunker.chunk(doc):
        headings = raw.meta.headings or []
        key = tuple(headings) if headings else ("Preâmbulo",)
        if key not in groups:
            groups[key] = []
        groups[key].append(raw)

    chunks: list[dict] = []
    categoria_ativa = "outros"

    for key, raw_list in groups.items():
        titulo = " > ".join(key[-2:]) if len(key) >= 2 else key[-1]

        texts = [rc.text for rc in raw_list if rc.text and rc.text.strip()]
        texto = _clean_ocr("\n\n".join(texts))

        categoria_detectada = mapear_categoria(titulo, texto)

        if categoria_detectada == "ignorar":
            continue

        if categoria_detectada != "outros":
            categoria_ativa = categoria_detectada

        if len(texto) < 30:
            continue

        all_doc_items = [item for rc in raw_list for item in rc.meta.doc_items]
        pagina_inicio, pagina_fim = _pages_from_doc_items(all_doc_items)

        primary_cat = categoria_detectada if categoria_detectada != "outros" else categoria_ativa

        if categoria_detectada == "outros":
            print(f"[herdado -> {primary_cat}]: '{titulo[:50]}'")

        prompt_origem = CATEGORIA_PARA_PROMPTS.get(primary_cat, "")

        if primary_cat in _HARD_SPLIT_CATEGORIAS and len(texto) >= _HARD_SPLIT_MIN_CHARS:
            sub = _hard_split(
                texto=texto, titulo=titulo, codigo_aviso=codigo_aviso, fonte=fonte,
                categoria=primary_cat, prompt_origem=prompt_origem,
                pagina_inicio=pagina_inicio, pagina_fim=pagina_fim,
            )
            if sub:
                for sc in sub:
                    sc["chunk_index"] = len(chunks)
                    chunks.append(sc)
                continue

        chunks.append({
            "titulo":        titulo,
            "secao":         titulo,
            "categoria":     primary_cat,
            "prompt_origem": prompt_origem,
            "codigo_aviso":  codigo_aviso,
            "fonte":         fonte,
            "chunk_index":   len(chunks),
            "pagina_inicio": pagina_inicio,
            "pagina_fim":    pagina_fim,
            "is_anexo":      "anexo" in normalize_text(titulo),
            "texto":         texto,
        })

    return chunks