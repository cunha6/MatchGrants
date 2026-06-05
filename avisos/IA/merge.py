"""Combina as respostas dos 6 prompts num único JSON e aplica correções pós-merge."""

import re

from .pydantic_models import Aviso, parse_p1, parse_p2, parse_p3, parse_p4, parse_p5, parse_p6


def _primeiro_nao_nulo(*dicts: dict) -> dict:
    """
    Para cada chave, usa o primeiro valor não-nulo/não-vazio encontrado.
    Permite que cada prompt seja autoritativo nos seus próprios campos
    sem precisar de listar cada campo explicitamente.
    """
    result = {}
    for key in set().union(*dicts):
        for d in dicts:
            val = d.get(key)
            if val not in (None, [], ""):
                result[key] = val
                break
    return result


def merge(r1: dict, r2: dict, r3: dict, r4: dict, r5: dict, r6: dict) -> dict:
    p1_info, bpas = parse_p1(r1)
    p2_info, fases, areas, fa    = parse_p2(r2)
    p3_info, taxas               = parse_p3(r3)
    p4_info, metod               = parse_p4(r4)
    p5_info, limites, penal      = parse_p5(r5)
    p6_info                      = parse_p6(r6)

    # Merge geral: campos de cada prompt são maioritariamente não-sobrepostos
    aviso_data = _primeiro_nao_nulo(p1_info, p2_info, p3_info, p4_info, p5_info, p6_info)

    # Caso especial: contacto — P3 tem prioridade, P1 complementa sem duplicar
    contacto_p3 = p3_info.get("contacto") or []
    contacto_p1 = p1_info.get("contacto") or []
    aviso_data["contacto"] = contacto_p3 + [c for c in contacto_p1 if c not in contacto_p3]


    # Para formas_pagamento — acumular de todos os prompts em vez de pegar o primeiro
    formas_p2 = p2_info.get("formas_pagamento") or []
    formas_p3 = p3_info.get("formas_pagamento") or []
    # Deduplicar e combinar
    seen = set()
    combined = []
    for item in formas_p3 + formas_p2:
        if item not in seen:
            seen.add(item)
            combined.append(item)
    aviso_data["formas_pagamento"] = combined

    aviso = Aviso.model_validate(aviso_data)

    final = {
        "Aviso":                     aviso.model_dump(),
        "Beneficiario_Por_Acao":     [b.model_dump() for b in bpas],
        "fases":                     [f.model_dump() for f in fases],
        "Area_Abrangida":            [a.model_dump() for a in areas],
        "Fase_Area":                 [fa_item.model_dump() for fa_item in fa],
        "Taxa_Financiamento":        [t.model_dump() for t in taxas],
        "Limite_Despesa":            [l.model_dump() for l in limites],
        "Penalizacao_Incumprimento": [p.model_dump() for p in penal],
        "Metodologia_Avaliacao":     [m.model_dump() for m in metod],
    }

    # --- Correções pós-merge ---

    # O LLM às vezes devolve taxas como string ("85.0") em vez de float (85.0)
    for taxa in final["Taxa_Financiamento"]:
        for campo in ("taxa_base", "majoracao_regional", "taxa_maxima_global", "limite_acumulacao_minimis"):
            val = taxa.get(campo)
            if isinstance(val, str):
                try:
                    taxa[campo] = float(val)
                except (ValueError, TypeError):
                    pass

    # Fase_Area sem fase explícita recebe "GLOBAL" em vez de null
    for fa_item in final["Fase_Area"]:
        if fa_item.get("codigo_fase") is None:
            fa_item["codigo_fase"] = "GLOBAL"

    # Se territorios_baixa_densidade estiver vazio, tenta inferir das penalizações
    if not final["Aviso"].get("territorios_baixa_densidade"):
        for pen in final["Penalizacao_Incumprimento"]:
            descricao = pen.get("descricao_regra") or ""
            if pen.get("limiar_tolerancia_baixa_densidade") is not None:
                match = re.search(r'delibera[çc][aã]o\s+[^\.,;]+', descricao, re.IGNORECASE)
                fonte_ref = match.group(0).strip() if match else "legislação aplicável"
                final["Aviso"]["territorios_baixa_densidade"] = [
                    f"Municípios de baixa densidade conforme {fonte_ref}"
                ]
                break
            if "baixa densidade" in descricao.lower():
                match = re.search(
                    r'delibera[çc][aã]o\s+(?:da\s+)?CIC\s+n[.º°]*\s*[\d/\.A-Z]+',
                    descricao, re.IGNORECASE,
                )
                ref = match.group(0).strip() if match else "deliberação CIC n.º 31/2023/PL"
                final["Aviso"]["territorios_baixa_densidade"] = [
                    f"Municípios de baixa densidade conforme {ref}"
                ]
                break

    # Avisos FSE+ não têm distinção infraestrutural — remove prefixos [Infra]/[Não-Infra]
    if "FSE" in (final["Aviso"].get("nome_fundo") or "").upper():
        metas = final["Aviso"].get("metas_execucao_financeira") or []
        final["Aviso"]["metas_execucao_financeira"] = [
            m.replace("[Infra] ", "").replace("[Não-Infra] ", "") for m in metas
        ]

    preenchido = sum(1 for v in final["Aviso"].values() if v not in (None, [], ""))
    print(f"  Aviso: {preenchido}/{len(final['Aviso'])} campos preenchidos")

    return final
