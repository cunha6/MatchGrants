"""
Ordenação dos matches por dotação e taxa de financiamento EFETIVAS.

Alguns avisos têm dotação/taxa diferentes consoante a FASE (ex: Fase 1/2/3) e a ÁREA
geográfica (ex: por CIM/AMP). Para ordenar de forma justa, escolhe-se a linha de PhaseArea
aplicável à empresa concreta:
  • fase ATIVA — a fase cujo intervalo de datas contém hoje; senão a próxima a abrir; senão
    a mais recente;
  • ÁREA da empresa — a CoveredArea que casa com a localização do cliente (ou a única, se só
    houver uma).
A partir dessa seleção extraem-se a dotação (maior pote disponível) e a taxa de financiamento
(a taxa de comparticipação real, ignorando a linha "Dotação Global" a 100%, que é o total
fundo+contrapartida e não a taxa que o beneficiário recebe).

Funções puras (operam sobre listas de dicts) — sem ORM, fáceis de testar.
"""

import re
import unicodedata
from datetime import datetime


def _norm(text) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


def _parse_dt(text):
    """Converte uma data ISO-ish ('2026-04-30T15:00:00' ou '2026-04-30') em datetime, ou None."""
    if not text:
        return None
    s = str(text).strip().replace("Z", "")
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None


def active_phase_id(phases: list[dict], now: datetime | None = None) -> int | None:
    """id (PK) da fase relevante agora: a que está a decorrer; senão a próxima a abrir;
    senão a mais recente. None se nenhuma fase tiver datas utilizáveis."""
    now = now or datetime.now()
    parsed = [(p.get("id"), _parse_dt(p.get("start_date")), _parse_dt(p.get("end_date")))
              for p in (phases or [])]

    for pid, s, e in parsed:
        if s and e and s <= now <= e:
            return pid
        if s and not e and s <= now:
            return pid

    upcoming = sorted((s, pid) for pid, s, e in parsed if s and s > now)
    if upcoming:
        return upcoming[0][1]

    ended = sorted((e or s, pid) for pid, s, e in parsed if (e or s))
    if ended:
        return ended[-1][1]
    return None


def company_area_id(client_tokens: list[str], covered_areas: list[dict]) -> int | None:
    """id (PK) da área que casa com a localização do cliente. Uma só área → essa.
    Vários → a primeira cujo nome geográfico contenha (ou esteja contido em) um token do cliente."""
    areas = covered_areas or []
    if len(areas) == 1:
        return areas[0].get("id")
    toks = [t for t in (client_tokens or []) if t]
    for a in areas:
        g = _norm(a.get("geographic_area"))
        if g and any(t in g or g in t for t in toks):
            return a.get("id")
    return None


def effective_budget_rate(phase_areas: list[dict], phase_id: int | None,
                          area_id: int | None) -> tuple[float | None, float | None]:
    """(dotação, taxa) efetivas para a fase/área escolhidas (ligação por FK: phase_id/area_id).

    - dotação = maior `budget_allocation` entre as linhas aplicáveis (inclui a Dotação Global
      quando existe — representa o total disponível).
    - taxa = maior `max_financing_rate` de comparticipação, ignorando a linha "Dotação Global"
      (100% = fundo+contrapartida, não é a taxa recebida). Se só existir a global, usa-a.
    """
    pool = phase_areas or []
    # 1) filtra por área (linhas sem área — area_id None — aplicam-se a todas)
    if area_id is not None:
        by_area = [pa for pa in pool if pa.get("area_id") in (area_id, None)]
        pool = by_area or pool
    # 2) se houver linhas da FASE ativa, usa-as; senão considera TODAS as aplicáveis (assim
    #    o caso fundo+"Dotação Global" mantém as duas linhas: dotação da global, taxa do fundo).
    phase_pool = [pa for pa in pool if phase_id is not None and pa.get("phase_id") == phase_id]
    chosen = phase_pool or pool

    budgets = [pa["budget_allocation"] for pa in chosen if pa.get("budget_allocation") is not None]
    fund_rates = [pa["max_financing_rate"] for pa in chosen
                  if pa.get("max_financing_rate") is not None
                  and _norm(pa.get("fund_name")) != "dotacao global"]
    all_rates = [pa["max_financing_rate"] for pa in chosen if pa.get("max_financing_rate") is not None]

    budget = max(budgets) if budgets else None
    rate = max(fund_rates) if fund_rates else (max(all_rates) if all_rates else None)
    return budget, rate


def _to_rate(value) -> float | None:
    """Extrai uma percentagem de um texto de taxa (ex: '60,0', '85%') em float, ou None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("%", "").replace(",", ".").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def max_financing_rate_from_rates(financing_rates: list[dict]) -> float | None:
    """Maior taxa (max_global_rate, senão base_rate) das FinancingRate — fallback quando não
    há PhaseArea com taxa."""
    rates = []
    for fr in financing_rates or []:
        r = _to_rate(fr.get("max_global_rate")) or _to_rate(fr.get("base_rate"))
        if r is not None:
            rates.append(r)
    return max(rates) if rates else None
