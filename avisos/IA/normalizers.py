"""Normalizações de dados aplicadas ao JSON extraído."""

import re
import unicodedata


def normalizar_codigo_aviso(codigo: str) -> str:
    """Remove espaços à volta de hífens: 'NORTE - 2024' -> 'NORTE-2024'."""
    if not codigo:
        return codigo
    return re.sub(r'\s*-\s*', '-', codigo.strip())


def extrair_codigo_aviso(r1: dict) -> str | None:
    """Tenta encontrar o codigo_aviso na resposta do Prompt 1."""
    try:
        if "codigo_aviso" in r1:
            return r1["codigo_aviso"]
        if "Aviso_Parte1" in r1:
            return r1["Aviso_Parte1"].get("codigo_aviso")
        if len(r1) == 1:
            inner = next(iter(r1.values()))
            if isinstance(inner, dict):
                return inner.get("codigo_aviso")
    except Exception:
        pass
    return None


def injetar_ancora(system_prompt: str, codigo: str | None) -> str:
    """Acrescenta o codigo_aviso ao prompt para garantir consistência entre os 6 prompts."""
    if not codigo:
        return system_prompt
    return system_prompt + (
        f'\n\nÂNCORA OBRIGATÓRIA: O codigo_aviso já foi determinado: "{codigo}". '
        "Usa EXATAMENTE este valor em todos os campos codigo_aviso do teu output. "
        "Não o reformates nem alteres."
    )


def normalizar_codigos_aviso_json(final: dict) -> dict:
    """Garante que o codigo_aviso é idêntico em todos os objetos do JSON final."""
    aviso = final.get("Aviso", {})
    codigo = aviso.get("codigo_aviso")
    if not codigo:
        return final

    codigo_norm = normalizar_codigo_aviso(codigo)
    aviso["codigo_aviso"] = codigo_norm

    listas_com_codigo = [
        "Beneficiario_Por_Acao", "fases",
        "Area_Abrangida", "Fase_Area", "Taxa_Financiamento",
        "Limite_Despesa", "Penalizacao_Incumprimento", "Metodologia_Avaliacao",
    ]
    for nome_lista in listas_com_codigo:
        for item in final.get(nome_lista, []):
            if "codigo_aviso" in item:
                item["codigo_aviso"] = codigo_norm

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