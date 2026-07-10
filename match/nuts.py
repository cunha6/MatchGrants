"""Resolução município → NUTS II / NUTS III a partir do nuts.json (autoritativo).

O nif.pt devolve a localização ao nível do concelho/distrito (ex: "Faro"), mas os avisos
usam a NUTS II ("Algarve"). Este módulo faz a correspondência EXATA — cada município tem a
sua NUTS II/III — carregada UMA vez do nuts.json para um dicionário em memória (278 municípios,
~78 KB; não justifica SQLite).
"""

import json
import unicodedata
from pathlib import Path

_NUTS_PATH = Path(__file__).resolve().parent.parent / "nuts.json"


def _norm(s) -> str:
    """minúsculas + sem acentos + espaços colapsados (para casar nomes de município)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _load() -> dict[str, dict]:
    try:
        with open(_NUTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    out: dict[str, dict] = {}
    for row in data:
        muni = _norm(row.get("municipio"))
        if muni:
            out[muni] = {"nuts2": row.get("nuts2"), "nuts3": row.get("nuts3")}
    return out


# Carregado uma vez ao importar o módulo.
_NUTS_BY_MUNICIPIO = _load()

# O nuts.json usa uma taxonomia de NUTS II mais fina ("Grande Lisboa", "Oeste"…); os avisos
# usam as 5 NUTS II continentais. Este mapa (por NUTS III) dá a NUTS II "antiga"/standard.
# Chaves normalizadas (minúsculas, sem acentos).
_NUTS3_TO_OLD_NUTS2 = {
    "oeste": "Centro",
    "medio tejo": "Centro",
    "leziria do tejo": "Alentejo",
    "grande lisboa": "Lisboa",
    "peninsula de setubal": "Lisboa",
}


def _old_nuts2(nuts3, nuts2):
    """NUTS II 'antiga'/standard (5 continentais) a partir da NUTS III; senão a própria nuts2."""
    return _NUTS3_TO_OLD_NUTS2.get(_norm(nuts3), nuts2)


def nuts_for(*names) -> tuple[str | None, str | None, str | None]:
    """(nuts_ii, nuts_iii, nuts_ii_old) para o primeiro `name` (município) que casar no nuts.json.

    - `nuts_ii`     : a NUTS II tal como está no nuts.json (pode ser "Grande Lisboa", "Oeste"…).
    - `nuts_iii`    : a NUTS III.
    - `nuts_ii_old` : a NUTS II standard (5 continentais), derivada da NUTS III — para casar
                      com os avisos que usam "NUTS II Lisboa"/"Centro"/"Alentejo"/etc.
    Aceita vários candidatos (ex: city, county) e devolve o primeiro que resolver.
    (None, None, None) se nenhum casar."""
    for name in names:
        row = _NUTS_BY_MUNICIPIO.get(_norm(name))
        if row:
            n2, n3 = row.get("nuts2"), row.get("nuts3")
            return n2, n3, _old_nuts2(n3, n2)
    return None, None, None
