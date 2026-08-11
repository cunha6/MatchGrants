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
from datetime import datetime

from common.text import normalize as _norm
from common.dates import parse_datetime as _parse_dt


def active_phase_id(phases: list[dict], now: datetime | None = None) -> int | None:
    """id (PK) da fase relevante agora: a que está a decorrer; senão a próxima a abrir;
    senão a mais recente. None se nenhuma fase tiver datas utilizáveis."""
    now = now or datetime.now()
    parsed = [(phase.get("id"), _parse_dt(phase.get("start_date")), _parse_dt(phase.get("end_date")))
              for phase in (phases or [])]

    for phase_id, start_date, end_date in parsed:
        if start_date and end_date and start_date <= now <= end_date:
            return phase_id
        if start_date and not end_date and start_date <= now:
            return phase_id

    upcoming = sorted((start_date, phase_id) for phase_id, start_date, end_date in parsed if start_date and start_date > now)
    if upcoming:
        return upcoming[0][1]

    ended = sorted((end_date or start_date, phase_id) for phase_id, start_date, end_date in parsed if (end_date or start_date))
    if ended:
        return ended[-1][1]
    return None


def company_area_id(client_tokens: list[str], covered_areas: list[dict]) -> int | None:
    """id (PK) da área que casa com a localização do cliente. Uma só área → essa.
    Vários → a primeira cujo nome geográfico contenha (ou esteja contido em) um token do cliente."""
    areas = covered_areas or []
    if len(areas) == 1:
        return areas[0].get("id")
    normalized_tokens = [token for token in (client_tokens or []) if token]
    for area in areas:
        normalized_area = _norm(area.get("geographic_area"))
        if normalized_area and any(token in normalized_area or normalized_area in token for token in normalized_tokens):
            return area.get("id")
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
        by_area = [phase_area for phase_area in pool if phase_area.get("area_id") in (area_id, None)]
        pool = by_area or pool
    # 2) se houver linhas da FASE ativa, usa-as; senão considera TODAS as aplicáveis (assim
    #    o caso fundo+"Dotação Global" mantém as duas linhas: dotação da global, taxa do fundo).
    phase_pool = [phase_area for phase_area in pool if phase_id is not None and phase_area.get("phase_id") == phase_id]
    chosen = phase_pool or pool

    budgets = [phase_area["budget_allocation"] for phase_area in chosen if phase_area.get("budget_allocation") is not None]
    fund_rates = [phase_area["max_financing_rate"] for phase_area in chosen
                  if phase_area.get("max_financing_rate") is not None
                  and _norm(phase_area.get("fund_name")) != "dotacao global"]
    all_rates = [phase_area["max_financing_rate"] for phase_area in chosen if phase_area.get("max_financing_rate") is not None]

    budget = max(budgets) if budgets else None
    rate = max(fund_rates) if fund_rates else (max(all_rates) if all_rates else None)
    return budget, rate


def _to_rate(value) -> float | None:
    """Extrai uma percentagem de um texto de taxa (ex: '60,0', '85%') em float, ou None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    start_date = str(value).replace("%", "").replace(",", ".").strip()
    m = re.search(r"\d+(?:\.\d+)?", start_date)
    return float(m.group()) if m else None


def max_financing_rate_from_rates(financing_rates: list[dict]) -> float | None:
    """Maior taxa (max_global_rate, senão base_rate) das FinancingRate — fallback quando não
    há PhaseArea com taxa."""
    rates = []
    for financing_rate in financing_rates or []:
        rate_value = _to_rate(financing_rate.get("max_global_rate")) or _to_rate(financing_rate.get("base_rate"))
        if rate_value is not None:
            rates.append(rate_value)
    return max(rates) if rates else None
