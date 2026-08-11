"""Cliente da API cttcodigopostal.pt — resolve um código postal em localidade/concelho/distrito.

O nif.pt dá o código postal (ex: "4800-937"); a CTT devolve a localização precisa (localidade,
concelho, distrito), que é depois usada para encontrar a NUTS II/III no nuts.json pela cidade.

Degrada graciosamente: sem CTT_KEY, código postal inválido, ou falha de rede → (None, None,
None) e o match cai para os dados do nif.pt/enriquecimento.
"""

import os

import requests

_URL = "https://www.cttcodigopostal.pt/api/v1/{key}/{cp}"
_TIMEOUT = 10


def _field(rec: dict, *names: str) -> str | None:
    """Valor do primeiro campo (case-insensitive) que exista e não seja vazio."""
    lower = {str(k).lower(): v for k, v in rec.items()}
    for n in names:
        v = lower.get(n)
        if v:
            return str(v).strip()
    return None


def ctt_lookup(postal_code) -> tuple[str | None, str | None, str | None]:
    """(localidade, concelho, distrito) para o código postal, via API CTT.

    (None, None, None) se não houver CTT_KEY, o código postal for vazio, ou a API falhar."""
    key = os.getenv("CTT_KEY", "")
    cp = (postal_code or "").strip()
    if not key or not cp:
        return None, None, None
    try:
        resp = requests.get(_URL.format(key=key, cp=cp), timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None, None, None

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None, None, None
    rec = data[0]
    return (
        _field(rec, "localidade", "local"),
        _field(rec, "concelho", "municipio"),
        _field(rec, "distrito", "distrito_ilha"),
    )
