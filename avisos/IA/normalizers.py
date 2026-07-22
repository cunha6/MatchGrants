"""Normalizações de dados aplicadas ao JSON extraído."""

import re

# Fonte única de normalização de texto — reexportada aqui porque o chunker e o
# documents.py importam `normalize_text` deste módulo.
from common.text import normalize as normalize_text  # noqa: F401


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


# "p.p." / "p.p" / "pontos percentuais" → "%": o utilizador quer SEMPRE "%", nunca "p.p".
# O lookbehind (?<![a-zA-Z]) evita apanhar "pp" no meio de palavras (ex: "supply", "app").
_PP_PATTERNS = [
    (re.compile(r"pontos?\s+percentuais?", re.IGNORECASE), "%"),
    (re.compile(r"ponto\s+percentual", re.IGNORECASE), "%"),
    (re.compile(r"(?<![a-zA-Z])p\.\s?p\.?", re.IGNORECASE), "%"),  # "p.p.", "p.p", "5p.p."
]


def _pp_str(text: str) -> str:
    if "://" in text:  # é um URL — não lhe tocar (evita corromper links)
        return text
    for pat, repl in _PP_PATTERNS:
        text = pat.sub(repl, text)
    return text


def normalize_pp_to_percent(obj):
    """Substitui 'p.p'/'pontos percentuais' por '%' em TODAS as strings do JSON (recursivo).
    Aplica-se ao resultado final para que tanto o JSON como a BD fiquem sempre com '%'."""
    if isinstance(obj, str):
        return _pp_str(obj)
    if isinstance(obj, list):
        return [normalize_pp_to_percent(x) for x in obj]
    if isinstance(obj, dict):
        return {k: normalize_pp_to_percent(v) for k, v in obj.items()}
    return obj


