"""Normalizações de dados aplicadas ao JSON extraído."""

import re
import unicodedata


def normalize_grant_code(code: str) -> str:
    if not code:
        return code
    return re.sub(r'\s*-\s*', '-', code.strip())


def extract_grant_code(r1: dict) -> str | None:
    """Tenta encontrar o grant_code na resposta do Prompt 1."""
    try:
        if "grant_code" in r1:
            return r1["grant_code"]
        if "Grant_Part1" in r1:
            return r1["Grant_Part1"].get("grant_code")
        if len(r1) == 1:
            inner = next(iter(r1.values()))
            if isinstance(inner, dict):
                return inner.get("grant_code")
    except Exception:
        pass
    return None


def inject_anchor(system_prompt: str, code: str | None) -> str:
    if not code:
        return system_prompt
    return system_prompt + (
        f'\n\nMANDATORY ANCHOR: The grant_code has already been determined: "{code}". '
        "Use EXACTLY this value in all grant_code fields of your output. "
        "Do not reformat or alter it."
    )


def normalize_grant_codes_json(final: dict) -> dict:
    grant_data = final.get("Grant", {})
    grant_code = grant_data.get("grant_code")
    if not grant_code:
        return final

    grant_code_norm = normalize_grant_code(grant_code)
    grant_data["grant_code"] = grant_code_norm

    lists_with_code = [
        "BeneficiaryByAction", "phases",
        "CoveredArea", "PhaseArea", "FinancingRate",
        "ExpenseLimit", "NonCompliancePenalty", "EvaluationMethodology",
    ]
    for list_name in lists_with_code:
        for item in final.get(list_name, []):
            if "grant_code" in item:
                item["grant_code"] = grant_code_norm

    return final


def normalize_text(text: str) -> str:
    """Remove acentos, converte para lowercase e limpa espaços extras."""
    if not text:
        return ""

    # 1. Remove acentos e passa a minúsculas
    nfkd = unicodedata.normalize('NFKD', text)
    clean_text = nfkd.encode('ascii', 'ignore').decode('ascii').lower()

    # 2. Transforma múltiplos espaços, tabs e quebras de linha (\n) num único espaço
    clean_text = re.sub(r'\s+', ' ', clean_text)

    return clean_text.strip()
