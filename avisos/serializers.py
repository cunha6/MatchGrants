"""Serialização dos avisos para as respostas da API.

Separado das views porque NÃO é lógica de HTTP e é consumido de fora delas: pelo digest de
avisos (avisos/notifications.py) e pela newsletter (newsletter/services.py). Enquanto vivia
em views.py, esses dois tinham de importar helpers privados de um módulo de views — a
newsletter de outra app, e o digest com um import local só para escapar a um ciclo.

`financing_rate`, `grant_summary`, `grant_detail` e `nearest_phase_date` são a API pública
deste módulo (sem underscore, ao contrário de quando estavam em views.py: eram privados no
nome mas importados de fora, o que era contraditório).
"""

import os
from datetime import date

from common.dates import parse_date
from common.files import safe_media_path
from match.ranking import _norm, max_financing_rate_from_rates
from .models import FinancingRate, Grant, PhaseArea

# Pasta (relativa ao BASE_DIR) onde ficam os PDFs dos avisos descarregados.
_AVISOS_DIR = "pdf_Avisos"

# Não expostos na resposta tal-qual (vetor de embedding/hash — ruído binário/interno;
# `last_updated_by` é uma FK — não serializável diretamente, tratada à parte em grant_detail).
_DETAIL_SKIP = {"activity_embedding", "activity_embedding_hash", "last_updated_by"}


# --- Helpers de ranking/serialização ---------------------------------------

def _computed_financing_rate(grant: Grant) -> float | None:
    """Taxa representativa CALCULADA das linhas-filhas: a maior taxa de comparticipação das
    linhas de dotação (ignorando a 'Dotação Global' a 100%, que é fundo+contrapartida);
    em falta, a maior das FinancingRate. None se o aviso não declarar taxa."""
    pa_rates = [
        phase_area.max_financing_rate for phase_area in grant.phase_areas.all()
        if phase_area.max_financing_rate is not None and _norm(phase_area.fund_name) != "dotacao global"
    ]
    if pa_rates:
        return max(pa_rates)
    return max_financing_rate_from_rates([
        {"max_global_rate": financing_rate_row.max_global_rate, "base_rate": financing_rate_row.base_rate}
        for financing_rate_row in grant.financing_rates.all()
    ])


def financing_rate(grant: Grant) -> float | None:
    """Taxa de financiamento EFETIVA mostrada: o override manual (`grant.financing_rate`)
    quando definido; senão a taxa calculada das linhas-filhas. É este valor que aparece na
    listagem e no detalhe e que serve para ordenar/filtrar por taxa."""
    if grant.financing_rate is not None:
        return grant.financing_rate
    return _computed_financing_rate(grant)


def nearest_phase_date(grant: Grant, today: date) -> date | None:
    """Data da fase MAIS PRÓXIMA: a data de fase mais cedo que ainda não passou; se todas já
    passaram, a mais recente. None se o aviso não tiver fases com datas legíveis."""
    dates = []
    for phase in grant.phases.all():
        for raw in (phase.start_date, phase.end_date):
            phase_date = parse_date(raw)
            if phase_date:
                dates.append(phase_date)
    if not dates:
        return None
    upcoming = [phase_date for phase_date in dates if phase_date >= today]
    return min(upcoming) if upcoming else max(dates)


def grant_summary(grant: Grant, rate: float | None, nearest: date | None) -> dict:
    """Resumo ENXUTO para a listagem (o front-end mostra só o essencial; o detalhe vem do GET
    /avisos/<id>/). Campos: código, título, data final, dotação, taxa de financiamento."""
    return {
        "id": grant.id,
        "grant_code": grant.grant_code,
        "title": grant.title,
        "closing_date": grant.closing_date,
        "total_allocation": grant.total_allocation,
        "financing_rate": rate,
        "next_phase_date": nearest.isoformat() if nearest else None,
        "active": grant.active,
    }


def _serialize_financing_rate(financing_rate_row: FinancingRate) -> dict:
    """Uma linha de taxa de financiamento (usada no detalhe e na edição de coleção)."""
    return {
        "company_size": financing_rate_row.company_size, "aid_regime": financing_rate_row.aid_regime, "base_rate": financing_rate_row.base_rate,
        "regional_bonus": financing_rate_row.regional_bonus, "max_global_rate": financing_rate_row.max_global_rate,
        "minimis_accumulation_limit": financing_rate_row.minimis_accumulation_limit,
        "specific_condition": financing_rate_row.specific_condition,
    }


def _serialize_phase_area(phase_area: PhaseArea) -> dict:
    """Uma linha de dotação/taxa por fase×área (usada no detalhe e na edição de coleção)."""
    return {
        "phase_id": phase_area.phase_id, "area_id": phase_area.area_id, "fund_name": phase_area.fund_name,
        "budget_allocation": phase_area.budget_allocation, "max_financing_rate": phase_area.max_financing_rate,
        "distribution": phase_area.distribution,
    }


def grant_detail(grant: Grant) -> dict:
    """Serialização COMPLETA do aviso (todos os campos + relações) para o GET de detalhe."""
    serialized = {model_field.name: getattr(grant, model_field.name)
            for model_field in grant._meta.fields if model_field.name not in _DETAIL_SKIP}
    # Quem/o quê fez a última escrita: 'scrape' (pipeline) ou 'manual' (edição) + o utilizador,
    # quando aplicável (username, não o objeto User — que não é serializável em JSON).
    serialized["last_updated_by"] = grant.last_updated_by.username if grant.last_updated_by_id else None
    # `financing_rate` = valor EFETIVO (override manual ou calculado). `financing_rate_manual`
    # diz ao front-end se está fixado à mão (para o formulário mostrar "auto" vs valor fixo).
    serialized["financing_rate"] = financing_rate(grant)
    serialized["financing_rate_manual"] = grant.financing_rate is not None
    serialized["phases"] = [
        {"id": phase.id, "name": phase.name, "start_date": phase.start_date,
         "end_date": phase.end_date, "access_condition": phase.access_condition}
        for phase in grant.phases.all()
    ]
    serialized["covered_areas"] = [
        {"id": a.id, "geographic_area": a.geographic_area} for a in grant.covered_areas.all()
    ]
    serialized["phase_areas"] = [_serialize_phase_area(phase_area) for phase_area in grant.phase_areas.all()]
    serialized["financing_rates"] = [_serialize_financing_rate(financing_rate_row) for financing_rate_row in grant.financing_rates.all()]
    serialized["expense_limits"] = [
        {"expense_category": expense_limit.expense_category,
         "applicable_ocs_methodology": expense_limit.applicable_ocs_methodology,
         "max_absolute_value": expense_limit.max_absolute_value,
         "max_percentage_value": expense_limit.max_percentage_value,
         "calculation_base": expense_limit.calculation_base, "specific_conditions": expense_limit.specific_conditions}
        for expense_limit in grant.expense_limits.all()
    ]
    serialized["non_compliance_penalties"] = [
        {"indicator_types": phase.indicator_types, "compliance_grade_formula": phase.compliance_grade_formula,
         "penalty_tiers": phase.penalty_tiers, "max_penalty_percentage": phase.max_penalty_percentage,
         "rule_description": phase.rule_description}
        for phase in grant.non_compliance_penalties.all()
    ]
    serialized["evaluation_methodologies"] = [
        {"project_merit_formula": methodology.project_merit_formula, "scoring_scale": methodology.scoring_scale,
         "min_global_score": methodology.min_global_score, "evaluation_criteria": methodology.evaluation_criteria,
         "tiebreaker_criteria": methodology.tiebreaker_criteria}
        for methodology in grant.evaluation_methodologies.all()
    ]
    serialized["beneficiaries_by_action"] = [
        {"action_type": beneficiary.action_type, "entities": beneficiary.entities}
        for beneficiary in grant.beneficiaries_by_action.all()
    ]
    serialized["documents"] = [
        {"doc_type": phase_date.doc_type, "name": phase_date.name, "url": phase_date.url, "is_canonical": phase_date.is_canonical}
        for phase_date in grant.documents.all()
    ]
    # O PDF do próprio aviso (descarregado em pdf_Avisos): link para abrir no browser.
    # `document_url` só vem preenchido se o ficheiro existir mesmo em disco. Também entra
    # como PRIMEIRO item da lista de documentos (o front-end abre-o com target="_blank").
    doc_url = f"/avisos/{grant.id}/document/" if safe_media_path(grant.pdf_path, _AVISOS_DIR) else None
    serialized["document_url"] = doc_url
    if doc_url:
        serialized["documents"].insert(0, {
            "doc_type": "aviso", "name": os.path.basename(grant.pdf_path),
            "url": doc_url, "is_canonical": True, "local": True,
        })
    return serialized
