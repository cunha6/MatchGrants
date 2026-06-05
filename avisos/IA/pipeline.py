"""
Orquestração do pipeline: chunking -> OpenAI (6 prompts) -> merge -> JSON.

Ponto de entrada: run_pipeline(doc, fonte, output_dir)
"""

import asyncio
import json
import time
from pathlib import Path

from .chunker import chunk_por_headers, CATS_P1, CATS_P2, CATS_P3, CATS_P4, CATS_P5, CATS_P6
from .merge import merge
from .normalizers import extrair_codigo_aviso, injetar_ancora, normalizar_codigo_aviso, normalizar_codigos_aviso_json, normalize_text
from .openai_client import classificar_chunks_ambiguos, chamar_openai, criar_cliente
from .prompts import SYSTEM_PROMPT_1, SYSTEM_PROMPT_2, SYSTEM_PROMPT_3, SYSTEM_PROMPT_4, SYSTEM_PROMPT_5, SYSTEM_PROMPT_6, SYSTEM_PROMPT_7

# Modelo usado em cada prompt
P7_MODEL = "gpt-5-mini-2025-08-07"
P1_MODEL = "gpt-5-mini-2025-08-07"
P2_MODEL = "gpt-4o-mini"
P3_MODEL = "gpt-5.4-mini-2026-03-17"
P4_MODEL = "gpt-5.4"
P5_MODEL = "gpt-4o-mini"
P6_MODEL = "gpt-4o-mini"


def _contar_vazios(final: dict) -> int:
    return sum(1 for v in final.get("Aviso", {}).values() if v in (None, [], ""))


_STOPWORDS = {"de", "da", "do", "dos", "das", "e", "em", "a", "o", "por", "para", "ao", "com"}

def _chunks_relevantes_para_vazios(chunks: list[dict], campos_vazios: list[str]) -> list[dict]:
    """Devolve chunks do corpo (não-Anexo) mais relevantes para os campos vazios.

    Pontua cada chunk pelo número de keywords dos nomes dos campos vazios que
    aparecem no título da secção ou na categoria do chunk.
    """
    if not campos_vazios:
        return []

    keywords = {
        w.lower()
        for campo in campos_vazios
        for w in campo.split("_")
        if w not in _STOPWORDS and len(w) > 2
    }

    scored: list[tuple[int, dict]] = []
    for c in chunks:
        if c.get("is_anexo") or c.get("categoria") == "ignorar":
            continue
        haystack = (
            (c.get("secao") or c.get("titulo") or "")
            + " "
            + (c.get("categoria") or "")
            + " "
            + (c.get("texto") or "")[:200]
        ).lower()
        score = sum(1 for kw in keywords if kw in haystack)
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



async def _run(doc, fonte: str, output_dir: Path) -> dict:
    json_dir = output_dir / "json"
    json_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'─' * 60}")
    print(f"  Documento: {fonte}")
    print(f"{'─' * 60}")

    # 1. Chunking
    print("\n[1/2] Chunking semântico")
    chunks = chunk_por_headers(doc, codigo_aviso=fonte, fonte=fonte)

    cats: dict[str, int] = {}
    for c in chunks:
        cats[c["categoria"]] = cats.get(c["categoria"], 0) + 1
    for cat, n in sorted(cats.items()):
        print(f"  [{cat}] {n} secção(ões)")
    print(f"  Total: {len(chunks)} chunks")

    # Chunks sem categoria são classificados pelo LLM antes de seguir para os prompts
    client = criar_cliente()
    outros = [c for c in chunks if c["categoria"] == "outros"]
    if outros:
        print(f"\n  [Router] {len(outros)} chunks ambíguos → LLM")
        routed = await classificar_chunks_ambiguos(client, outros)
        n_reclassificados = 0
        for chunk in chunks:
            if chunk["categoria"] != "outros":
                continue
            cats_llm = routed.get(str(chunk.get("chunk_index", "")), [])
            if cats_llm and cats_llm[0] not in ("ignorar", "outros"):
                chunk["categoria"] = cats_llm[0]
                n_reclassificados += 1
        print(f"  [Router] {n_reclassificados}/{len(outros)} reclassificados")

    # Chunks que ficaram sem categoria vão como fallback para P1
    fallback = [c for c in chunks if c["categoria"] == "outros"]

    # Distribui os chunks pelos 6 prompts conforme as categorias do mapping_config.json
    p1_chunks = [c for c in chunks if c["categoria"] in CATS_P1] + fallback
    p2_chunks = [c for c in chunks if c["categoria"] in CATS_P2]
    p3_chunks = [c for c in chunks if c["categoria"] in CATS_P3]
    p4_chunks = [c for c in chunks if c["categoria"] in CATS_P4]
    p5_chunks = [c for c in chunks if c["categoria"] in CATS_P5]
    p6_chunks = [c for c in chunks if c["categoria"] in CATS_P6]

    # Garante que anexos com critérios e legislação chegam sempre aos prompts certos,
    # mesmo que o chunker os tenha categorizado incorrectamente
    _ids_p4 = {id(c) for c in p4_chunks}
    _ids_p1 = {id(c) for c in p1_chunks}
    for c in chunks:
        if not c.get("is_anexo"):
            continue
        txt = (c.get("titulo", "") + " " + c.get("texto", "")[:300]).lower()
        if id(c) not in _ids_p4 and any(kw in txt for kw in (
            "grelha", "referencial de mérito", "critérios de seleção",
            "critérios de avaliação", "metodologia de avaliação", "ponderaç",
        )):
            p4_chunks.append(c)
        if id(c) not in _ids_p1 and any(kw in txt for kw in (
            "legislação", "regulamento", "decreto-lei", "portaria", "diploma",
        )):
            p1_chunks.append(c)

    # 2. OpenAI — P1 primeiro para obter o codigo_aviso, depois P2–P6 em paralelo
    print("\n[2/2] OpenAI")

    r1 = await chamar_openai(client, SYSTEM_PROMPT_1, p1_chunks, "P1 Identificação", P1_MODEL)

    codigo_aviso = extrair_codigo_aviso(r1)
    if codigo_aviso:
        codigo_aviso = normalizar_codigo_aviso(codigo_aviso)
        print(f"  Âncora: {codigo_aviso!r}")
    else:
        print("  AVISO: codigo_aviso não encontrado em P1")

    print("\n  P2–P6 em paralelo ...")
    t = time.time()
    r2, r3, r4, r5, r6 = await asyncio.gather(
        chamar_openai(client, injetar_ancora(SYSTEM_PROMPT_2, codigo_aviso), p2_chunks, "P2 Território+Fases",     P2_MODEL),
        chamar_openai(client, injetar_ancora(SYSTEM_PROMPT_3, codigo_aviso), p3_chunks, "P3 Taxas+Pagamentos",     P3_MODEL),
        chamar_openai(client, injetar_ancora(SYSTEM_PROMPT_4, codigo_aviso), p4_chunks, "P4 Critérios+Grelhas",    P4_MODEL),
        chamar_openai(client, injetar_ancora(SYSTEM_PROMPT_5, codigo_aviso), p5_chunks, "P5 Despesas+Indicadores", P5_MODEL),
        chamar_openai(client, injetar_ancora(SYSTEM_PROMPT_6, codigo_aviso), p6_chunks, "P6 Documentos",           P6_MODEL),
    )
    print(f"\n  Concluído em {time.time()-t:.1f}s")

    # Merge e normalização final
    print("\n  Merge:")
    final = merge(r1, r2, r3, r4, r5, r6)
    final = normalizar_codigos_aviso_json(final)

    # P7 — enriquecer com Anexos + preencher campos vazios do corpo
    anexo_chunks = [
        c for c in chunks
        if c.get("is_anexo") and c.get("categoria") != "ignorar"
    ]

    campos_vazios = [k for k, v in final["Aviso"].items() if v in (None, [], "")]
    corpo_chunks = _chunks_relevantes_para_vazios(chunks, campos_vazios)

    p7_chunks = anexo_chunks + corpo_chunks

    if p7_chunks:
        campos_antes = _contar_vazios(final)
        print(f"\n  [P7] {len(anexo_chunks)} Anexos + {len(corpo_chunks)} corpo | {campos_antes} campos vazios")
        json_completo = json.dumps(final, ensure_ascii=False, indent=2)
        campos_vazios_str = ", ".join(f"`{c}`" for c in campos_vazios) if campos_vazios else "nenhum"
        system_p7 = (
            SYSTEM_PROMPT_7
            + f'\n\nCAMPOS VAZIOS A TENTAR PREENCHER: {campos_vazios_str}'
            + f'\n\nJSON ACTUAL (referência — devolve apenas o que alterares):\n{json_completo}'
        )
        r7 = await chamar_openai(client, system_p7, p7_chunks, "P7 Enriquecer Anexos", P7_MODEL)
        alteracoes = r7.get("alteracoes", {}) if isinstance(r7, dict) else {}
        if alteracoes:
            aviso_atualizados = []
            for campo, valor in alteracoes.get("Aviso", {}).items():
                if valor in (None, [], ""):
                    continue
                campo_atual = final["Aviso"].get(campo)
                # listas: P7 devolve sempre a versão merged (existentes + novos) → aplica sempre
                # escalares: só preenche se estiver vazio
                if isinstance(valor, list) or campo_atual in (None, [], ""):
                    final["Aviso"][campo] = valor
                    aviso_atualizados.append(campo)
            listas_atualizadas = []
            for chave in ("Area_Abrangida", "Fase_Area", "Metodologia_Avaliacao", "Taxa_Financiamento", "Limite_Despesa"):
                if alteracoes.get(chave):
                    final[chave] = alteracoes[chave]
                    listas_atualizadas.append(chave)
            final = normalizar_codigos_aviso_json(final)
            campos_depois = _contar_vazios(final)
            if aviso_atualizados:
                print(f"  [P7] Aviso preencheu: {aviso_atualizados}")
            if listas_atualizadas:
                print(f"  [P7] Listas atualizadas: {listas_atualizadas}")
            print(f"  [P7] {campos_antes - campos_depois} campos Aviso preenchidos ({campos_depois} ainda vazios)")
        else:
            print("  [P7] Sem alterações — mantém resultado do merge")

        nao_capturado = r7.get("nao_capturado", []) if isinstance(r7, dict) else []
        if nao_capturado:
            final["Aviso"]["aprofundar"] = nao_capturado
            print(f"  [P7] {len(nao_capturado)} temas para aprofundar → Aviso.aprofundar")
    else:
        print("\n  [P7] Sem chunks relevantes — skipped")

    json_file = json_dir / f"{fonte}.json"
    json_file.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  JSON guardado: {json_file}")

    return final


def run_pipeline(doc, fonte: str, output_dir: str = "output") -> dict:
    """
    Ponto de entrada principal.

    Parâmetros:
        doc        — DoclingDocument devolvido por result.document após _docling.convert()
        fonte      — nome do documento (ex: nome do PDF sem extensão)
        output_dir — pasta onde guardar o JSON resultante

    Exemplo:
        result = _docling.convert("aviso.pdf")
        dados  = run_pipeline(result.document, "aviso", output_dir="output")
    """
    return asyncio.run(_run(doc, fonte, Path(output_dir)))
