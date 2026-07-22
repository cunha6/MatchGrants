"""Normalização de texto — fonte ÚNICA para toda a aplicação.

Antes existiam cinco implementações quase iguais (match/nuts, match/scoring_rules,
match/ranking, avisos/IA/normalizers, anuncios/specifications); qualquer diferença subtil
entre elas era um bug à espera (ex: um matcher a normalizar acentos de forma diferente do
texto que compara). Todas passaram a importar daqui.
"""

import re
import unicodedata

_WS = re.compile(r"\s+")


def normalize(text) -> str:
    """minúsculas + sem acentos + espaços/quebras colapsados num único espaço.

    None → "". Não-strings são convertidas com str() (tolerante a números/objetos).
    """
    if text is None:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return _WS.sub(" ", t).lower().strip()
