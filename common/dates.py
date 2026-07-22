"""Interpretação tolerante de datas em texto livre — fonte ÚNICA para toda a aplicação.

As datas dos avisos/anúncios vêm de HTML, PDFs e APIs em formatos variados (ISO,
DD/MM/AAAA, AAAA/MM/DD, com hora, embebidas em frases como "até 30/09/2026 às 18h").
Antes havia três parsers independentes (avisos/service, match/ranking, anuncios/services);
este módulo junta-os para que todos aceitem exatamente os mesmos formatos.
"""

import re
from datetime import date, datetime

_DMY = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")   # DD/MM/AAAA ou DD-MM-AAAA
_YMD = re.compile(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b")   # AAAA/MM/DD ou AAAA-MM-DD


def parse_date(value) -> date | None:
    """Data contida em `value` (texto livre), ou None se não houver nenhuma legível.

    Aceita ISO (2026-09-30[T18:00[:00]][Z]), DD/MM/AAAA e AAAA/MM/DD ('/' ou '-'),
    mesmo embebidas numa frase. Datas impossíveis (ex: 32/13/2026) → None.
    """
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s[:19].replace("Z", "")).date()
    except ValueError:
        pass
    m = _DMY.search(s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
    else:
        m = _YMD.search(s)
        if not m:
            return None
        y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_datetime(value) -> datetime | None:
    """Datetime ISO-ish ('2026-04-30T15:00:00' ou '2026-04-30') em datetime, ou None.

    Usado onde a HORA importa (ex: fase ativa de um aviso — a candidatura fecha às 18h).
    """
    if not value:
        return None
    s = str(value).strip().replace("Z", "")
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
