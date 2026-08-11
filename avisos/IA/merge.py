"""Combina as respostas dos 6 prompts num único JSON e aplica correções pós-merge."""

import logging
import re

from .pydantic_models import Grant, parse_p1, parse_p2, parse_p3, parse_p4, parse_p5, parse_p6

logger = logging.getLogger(__name__)


def _first_non_null(*dicts: dict) -> dict:
    """
    Para cada chave, usa o primeiro valor não-nulo/não-vazio encontrado.
    Permite que cada prompt seja autoritativo nos seus próprios campos
    sem precisar de listar cada campo explicitamente.
    """
    result = {}
    for key in set().union(*dicts):
        for chunk_result in dicts:
            val = chunk_result.get(key)
            if val not in (None, [], ""):
                result[key] = val
                break
    return result


def merge(r1: dict, r2: dict, r3: dict, r4: dict, r5: dict, r6: dict) -> dict:
    p1_info, bpas = parse_p1(r1)
    p2_info, phases, areas, fa = parse_p2(r2)
    p3_info, rates = parse_p3(r3)
    p4_info, methodologies = parse_p4(r4)
    p5_info, limits, penalties = parse_p5(r5)
    p6_info = parse_p6(r6)

    # Valida cada dict parcial pelo modelo Grant para obter chaves em inglês
    p1_data, p2_data, p3_data, p4_data, p5_data = (
        Grant.model_validate(prompt_result).model_dump()
        for prompt_result in (p1_info, p2_info, p3_info, p4_info, p5_info)
    )
    p6_data = p6_info

    # Merge geral: campos de cada prompt são maioritariamente não-sobrepostos
    notice_data = _first_non_null(p1_data, p2_data, p3_data, p4_data, p5_data, p6_data)

    # contact — P3 tem prioridade, P1 complementa; payment_methods acumula P3+P2.
    # dict.fromkeys preserva a ordem e remove duplicados.
    notice_data["contact"] = list(dict.fromkeys(
        (p3_data.get("contact") or []) + (p1_data.get("contact") or [])
    ))
    notice_data["payment_methods"] = list(dict.fromkeys(
        (p3_data.get("payment_methods") or []) + (p2_data.get("payment_methods") or [])
    ))

    # application_documents vêm do P6
    if p6_data.get("application_documents"):
        notice_data["application_documents"] = p6_data["application_documents"]

    grant = Grant.model_validate(notice_data)

    result = {
        "Grant":                  grant.model_dump(),
        "BeneficiaryByAction":    [second.model_dump() for second in bpas],
        "phases":                 [phase.model_dump() for phase in phases],
        "CoveredArea":            [first.model_dump() for first in areas],
        "PhaseArea":              [fa_item.model_dump() for fa_item in fa],
        "FinancingRate":          [rate.model_dump() for rate in rates],
        "ExpenseLimit":           [lim.model_dump() for lim in limits],
        "NonCompliancePenalty":   [penalty.model_dump() for penalty in penalties],
        "EvaluationMethodology":  [methodology.model_dump() for methodology in methodologies],
    }

    # --- Correções pós-merge ---

    # O LLM às vezes devolve taxas como string ("85.0") em vez de float (85.0)
    for taxa in result["FinancingRate"]:
        for field in ("base_rate", "regional_bonus", "max_global_rate", "minimis_accumulation_limit"):
            val = taxa.get(field)
            if isinstance(val, str):
                try:
                    taxa[field] = float(val)
                except (ValueError, TypeError):
                    pass

    # PhaseArea sem fase explícita recebe "GLOBAL" em vez de null
    for fa_item in result["PhaseArea"]:
        if fa_item.get("phase_code") is None:
            fa_item["phase_code"] = "GLOBAL"

    # Se low_density_territories estiver vazio, tenta inferir das penalizações
    if not result["Grant"].get("low_density_territories"):
        for pen in result["NonCompliancePenalty"]:
            description = pen.get("rule_description") or ""
            if pen.get("low_density_tolerance_threshold") is not None:
                match = re.search(r'delibera[çc][aã]o\s+[^\.,;]+', description, re.IGNORECASE)
                source_ref = match.group(0).strip() if match else "legislação aplicável"
                result["Grant"]["low_density_territories"] = [
                    f"Municípios de baixa densidade conforme {source_ref}"
                ]
                break
            if "baixa densidade" in description.lower():
                match = re.search(
                    r'delibera[çc][aã]o\s+(?:da\s+)?CIC\s+n[.º°]*\s*[\d/\.A-Z]+',
                    description, re.IGNORECASE,
                )
                ref = match.group(0).strip() if match else "deliberação CIC n.º 31/2023/PL"
                result["Grant"]["low_density_territories"] = [
                    f"Municípios de baixa densidade conforme {ref}"
                ]
                break

    # Avisos FSE+ não têm distinção infraestrutural — remove prefixos [Infra]/[Não-Infra]
    if "FSE" in (result["Grant"].get("fund_name") or "").upper():
        targets = result["Grant"].get("financial_execution_targets") or []
        result["Grant"]["financial_execution_targets"] = [
            methodology.replace("[Infra] ", "").replace("[Não-Infra] ", "") for methodology in targets
        ]

    filled_count = sum(1 for field_value in result["Grant"].values() if field_value not in (None, [], ""))
    logger.prompt_result("Grant: %d/%d campos preenchidos", filled_count, len(result["Grant"]))

    return result
