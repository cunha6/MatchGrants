"""
Orquestração do pipeline: chunking -> OpenAI (6 prompts) -> merge -> JSON.

Ponto de entrada: run_pipeline(markdown, source, output_dir)
"""

import asyncio
import json
import time
from pathlib import Path

from .chunker import chunk_by_markdown, CATS_P1, CATS_P2, CATS_P3, CATS_P4, CATS_P5, CATS_P6
from .merge import merge
from .normalizers import extract_grant_code, inject_anchor, normalize_grant_code, normalize_grant_codes_json
from .openai_client import classify_ambiguous_chunks, call_openai, call_openai_text, create_client
from .prompts import SYSTEM_PROMPT_1, SYSTEM_PROMPT_2, SYSTEM_PROMPT_3, SYSTEM_PROMPT_4, SYSTEM_PROMPT_5, SYSTEM_PROMPT_6, SYSTEM_PROMPT_7, SYSTEM_PROMPT_CONSOLIDATE

# Modelo usado em cada prompt
P1_MODEL = "gpt-5-mini-2025-08-07"
P2_MODEL = "gpt-4o-mini"
P3_MODEL = "gpt-5.4-mini-2026-03-17"
P4_MODEL = "gpt-5.4"
P5_MODEL = "gpt-4o-mini"
P6_MODEL = "gpt-4o-mini"
P7_MODEL = "gpt-5-mini-2025-08-07"

_STOPWORDS = {"de", "da", "do", "dos", "das", "e", "em", "a", "o", "por", "para", "ao", "com"}

async def _run(markdown: str, source: str, output_dir: Path, extra: dict | None = None) -> dict:
    json_dir = output_dir / "json"
    json_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'─' * 60}")
    print(f"  Documento: {source}")
    print(f"{'─' * 60}")

    # 1. Chunking
    print("\n[1/2] Chunking semântico")
    chunks = chunk_by_markdown(markdown, grant_code=source, source=source)

    cats: dict[str, int] = {}
    for c in chunks:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    for cat, n in sorted(cats.items()):
        print(f"  [{cat}] {n} secção(ões)")
    print(f"  Total: {len(chunks)} chunks")

    # Routing multi-label: o LLM classifica TODOS os chunks. A categoria por keyword
    # (chunker) é unida com as categorias do LLM — um chunk pode pertencer a vários prompts.
    client = create_client()
    print(f"\n  [Router] a classificar {len(chunks)} chunks → LLM")
    routed = await classify_ambiguous_chunks(client, chunks)
    for chunk in chunks:
        labels: set[str] = set()
        kw = chunk.get("category")
        if kw and kw not in ("outros", "ignorar"):
            labels.add(kw)
        for lc in routed.get(str(chunk.get("chunk_index", "")), []):
            if lc not in ("outros", "ignorar"):
                labels.add(lc)
        chunk["categories"] = labels

    # Chunks sem qualquer categoria → fallback para P1 (rede de segurança)
    fallback = [c for c in chunks if not c["categories"]]
    n_multi = sum(1 for c in chunks if len(c["categories"]) > 1)
    print(f"  [Router] {n_multi} chunks multi-categoria, {len(fallback)} sem categoria (→P1)")

    # Distribui os chunks pelos 6 prompts por interseção de categorias
    p1_chunks = [c for c in chunks if c["categories"] & CATS_P1] + fallback
    p2_chunks = [c for c in chunks if c["categories"] & CATS_P2]
    p3_chunks = [c for c in chunks if c["categories"] & CATS_P3]
    p4_chunks = [c for c in chunks if c["categories"] & CATS_P4]
    p5_chunks = [c for c in chunks if c["categories"] & CATS_P5]
    p6_chunks = [c for c in chunks if c["categories"] & CATS_P6]

    # Rede de segurança final: garante que anexos temáticos chegam SEMPRE ao prompt certo,
    # mesmo que keyword e LLM tenham falhado. Cada anexo é encaminhado por conteúdo.
    _ANNEX_GUARANTEE = (
        (("grelha", "referencial de mérito", "critérios de seleção", "critérios de avaliação",
          "metodologia de avaliação", "ponderaç", "subcritério"), p4_chunks),
        (("legislação", "regulamento", "decreto-lei", "portaria", "diploma"), p1_chunks),
        (("documentos necessários", "memória descritiva", "documentos a apresentar"), p6_chunks),
        (("despesa", "custos elegíveis", "ocs", "indicador"), p5_chunks),
        (("metas de execução", "execução financeira", "taxa de execução", "anexo 6"), p2_chunks),
    )
    for c in chunks:
        if not c.get("is_annex"):
            continue
        txt = (c.get("title", "") + " " + c.get("text", "")[:300]).lower()
        for keywords, target in _ANNEX_GUARANTEE:
            if id(c) not in {id(x) for x in target} and any(kw in txt for kw in keywords):
                target.append(c)

    # 2. OpenAI — P1 primeiro para obter o notice_code, depois P2–P6 em paralelo
    print("\n[2/2] OpenAI")

    r1 = await call_openai(client, SYSTEM_PROMPT_1, p1_chunks, "P1 Identificação", P1_MODEL)

    notice_code = extract_grant_code(r1)
    if notice_code:
        notice_code = normalize_grant_code(notice_code)
        print(f"  Âncora: {notice_code!r}")
    else:
        print("  WARNING: grant_code not found in P1")

    print("\n  P2–P6 em paralelo ...")
    t = time.time()
    r2, r3, r4, r5, r6 = await asyncio.gather(
        call_openai(client, inject_anchor(SYSTEM_PROMPT_2, notice_code), p2_chunks, "P2 Território+Fases",     P2_MODEL),
        call_openai(client, inject_anchor(SYSTEM_PROMPT_3, notice_code), p3_chunks, "P3 Taxas+Pagamentos",     P3_MODEL),
        call_openai(client, inject_anchor(SYSTEM_PROMPT_4, notice_code), p4_chunks, "P4 Critérios+Grelhas",    P4_MODEL),
        call_openai(client, inject_anchor(SYSTEM_PROMPT_5, notice_code), p5_chunks, "P5 Despesas+Indicadores", P5_MODEL),
        call_openai(client, inject_anchor(SYSTEM_PROMPT_6, notice_code), p6_chunks, "P6 Documentos",           P6_MODEL),
    )
    print(f"\n  Concluído em {time.time()-t:.1f}s")

    # Merge e normalização final
    print("\n  Merge:")
    result = merge(r1, r2, r3, r4, r5, r6)
    result = normalize_grant_codes_json(result)

    # P7 — enriquecer com Anexos + preencher campos vazios do corpo
    annex_chunks = [
        c for c in chunks
        if c.get("is_annex") and c.get("category") != "ignorar"
    ]

    empty_fields = [k for k, v in result["Grant"].items() if v in (None, [], "")]
    body_chunks = _chunks_for_empty_fields(chunks, empty_fields)

    p7_chunks = annex_chunks + body_chunks

    if p7_chunks:
        fields_before = _count_empty_fields(result)
        print(f"\n  [P7] {len(annex_chunks)} Anexos + {len(body_chunks)} corpo | {fields_before} campos vazios")
        json_completo = json.dumps(result, ensure_ascii=False, indent=2)
        empty_fields_str = ", ".join(f"`{c}`" for c in empty_fields) if empty_fields else "nenhum"
        system_p7 = (
            SYSTEM_PROMPT_7
            + f'\n\nCAMPOS VAZIOS A TENTAR PREENCHER: {empty_fields_str}'
            + f'\n\nJSON ACTUAL (referência — devolve apenas o que alterares):\n{json_completo}'
        )
        r7 = await call_openai(client, system_p7, p7_chunks, "P7 Enriquecer Anexos", P7_MODEL)
        changes = r7.get("changes", {}) if isinstance(r7, dict) else {}
        if changes:
            updated_fields = []
            for field, value in changes.get("Grant", {}).items():
                if value in (None, [], ""):
                    continue
                current_value = result["Grant"].get(field)
                # listas: P7 devolve sempre a versão merged (existentes + novos) → aplica sempre
                # escalares: só preenche se estiver vazio
                if isinstance(value, list) or current_value in (None, [], ""):
                    result["Grant"][field] = value
                    updated_fields.append(field)
            updated_lists = []
            for key in ("CoveredArea", "PhaseArea", "EvaluationMethodology", "FinancingRate", "ExpenseLimit"):
                if changes.get(key):
                    result[key] = changes[key]
                    updated_lists.append(key)
            result = normalize_grant_codes_json(result)
            fields_after = _count_empty_fields(result)
            if updated_fields:
                print(f"  [P7] Aviso preencheu: {updated_fields}")
            if updated_lists:
                print(f"  [P7] Listas atualizadas: {updated_lists}")
            print(f"  [P7] {fields_before - fields_after} campos Aviso preenchidos ({fields_after} ainda vazios)")
        else:
            print("  [P7] Sem alterações — mantém resultado do merge")

        not_captured = r7.get("not_captured", []) if isinstance(r7, dict) else []
        if not_captured:
            result["Grant"]["to_explore"] = not_captured
            print(f"  [P7] {len(not_captured)} temas para aprofundar → Grant.to_explore")
    else:
        print("\n  [P7] Sem chunks relevantes — skipped")

    if extra:
        result.update(extra)

    json_file = json_dir / f"{source}.json"
    json_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  JSON guardado: {json_file}")

    return result

# Termos PT por campo: os nomes de campo são EN mas o texto dos chunks é PT. Sem este mapa,
# "financial_execution_targets".split("_") → {financial,execution,targets} nunca casa no
# texto PT e o rescue do P7 fica morto.
FIELD_PT_TERMS = {
    "financial_execution_targets": ["metas de execução", "taxa de execução", "execução financeira"],
    "state_aid_regime": ["auxílios de estado", "de minimis", "rgic", "651/2014"],
    "low_density_territories": ["baixa densidade", "territórios de baixa densidade"],
    "required_self_financing_limit": ["capitais próprios", "autofinanciamento"],
    "maximum_investment": ["apoio máximo", "investimento máximo", "máximo por candidatura"],
    "monitoring_indicators": ["indicadores de acompanhamento"],
}


def _field_search_terms(field: str) -> list[str]:
    """Termos PT a procurar para um campo vazio (fallback aos tokens >2 chars do nome)."""
    if field in FIELD_PT_TERMS:
        return FIELD_PT_TERMS[field]
    return [w.lower() for w in field.split("_") if w not in _STOPWORDS and len(w) > 2]


def _chunks_for_empty_fields(chunks: list[dict], empty_fields: list[str]) -> list[dict]:
    """Devolve chunks do corpo (não-Anexo) mais relevantes para os campos vazios.

    Pontua cada chunk pelo número de termos PT (de FIELD_PT_TERMS) dos campos vazios
    que aparecem no título/categoria/texto do chunk.
    """
    if not empty_fields:
        return []

    terms = {t for field in empty_fields for t in _field_search_terms(field)}

    scored: list[tuple[int, dict]] = []
    for c in chunks:
        if c.get("is_annex") or c.get("category") == "ignorar":
            continue
        data = (
            (c.get("section") or c.get("titulo") or "")
            + " "
            + (c.get("category") or "")
            + " "
            + (c.get("text") or "")[:200]
        ).lower()
        score = sum(1 for t in terms if t in data)
        if score > 0:
            scored.append((score, c))

    scored.sort(key=lambda x: -x[0])
    seen: set[int] = set()
    result: list[dict] = []
    for _, c in scored:
        cid = id(c)
        if cid not in seen:
            seen.add(cid)
            result.append(c)
    return result


def run_pipeline(markdown: str, source: str, output_dir: str = "output", extra: dict | None = None) -> dict:
    """
    Ponto de entrada principal.

    Parâmetros:
        markdown   — texto markdown do aviso (já convertido/consolidado)
        source     — nome do documento (ex: nome do PDF sem extensão)
        output_dir — pasta onde guardar o JSON resultante
        extra      — dados a juntar ao JSON final (ex: lista de anexos url+nome)

    Exemplo:
        markdown = converter pdf -> markdown
        dados    = run_pipeline(markdown, "aviso", output_dir="output")
    """
    return asyncio.run(_run(markdown, source, Path(output_dir), extra))


async def _consolidate(base_md: str, amendment_mds: list[str]) -> str:
    client = create_client()
    parts = [f"TEXTO BASE:\n{base_md}"]
    for i, amd in enumerate(amendment_mds, 1):
        parts.append(f"DOCUMENTO DE ALTERAÇÃO {i}:\n{amd}")
    user = "\n\n=====\n\n".join(parts)
    return await call_openai_text(
        client, SYSTEM_PROMPT_CONSOLIDATE, user, "Consolidação", P1_MODEL
    )

def _count_empty_fields(result: dict) -> int:
    return sum(1 for v in result.get("Grant", {}).values() if v in (None, [], ""))

def consolidate_markdowns(base_md: str, amendment_mds: list[str]) -> str:
    """
    Aplica os documentos de alteração (diffs) sobre o markdown base e devolve o
    markdown consolidado. Se não houver alterações, devolve o base inalterado.
    """
    if not amendment_mds:
        return base_md
    return asyncio.run(_consolidate(base_md, amendment_mds))
