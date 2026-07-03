import re

from .models import (
    Grant, BeneficiaryByAction, Phase, CoveredArea,
    PhaseArea, FinancingRate, ExpenseLimit, NonCompliancePenalty,
    EvaluationMethodology,
)

# Campos cuja fonte de verdade é o scrape do HTML. A IA nunca os sobrescreve.
_HTML_LOCKED_FIELDS = (
    "publication_date", "opening_date", "closing_date", "total_allocation",
)


def _parse_allocation(value) -> float | None:
    """Converte a 'Dotação' do HTML em float. Pontos = separador de milhares (removidos),
    vírgula = separador decimal. Ex: '1.500.000,50 €' -> 1500000.5. Se não der, None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.,]", "", str(value)).replace(".", "").replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None

_GRANT_AI_FIELDS = [
    "grant_code", "title", "financing_program", "managing_entity",
    "publication_date", "republication_date", "last_republication", "amendment_date",
    "notice_modality", "objective", "fund_name", "program_priority",
    "intervention_type_code", "max_duration_months",
    "included_caes", "excluded_caes",
    "eligible_regions", "expense_eligibility_start_date", "specific_objective",
    "operation_typology", "covered_actions", "intermediate_bodies",
    "applicable_legislation", "regulatory_documents", "target_technology_sectors",
    "application_submission", "beneficiary_eligibility_criteria",
    "operation_eligibility_criteria", "admissibility_conditions", "final_recipients",
    "dnsh_principle", "commitment_requirements", "total_allocation",
    "low_density_territories", "submission_limits", "absolute_execution_deadline",
    "financial_execution_targets", "minimum_investment", "maximum_investment",
    "required_self_financing_limit", "state_aid_regime", "applicable_gber_article",
    "contact", "payment_methods", "project_selection_criteria", "eligible_expenses",
    "ineligible_expenses", "output_indicators", "result_indicators",
    "monitoring_indicators", "beneficiary_obligations", "communication_obligations",
    "application_documents", "to_explore", "bonus_mechanisms", "dnsh_criteria",
]


def save_scraped_grant(grant_dict: dict, source: str) -> Grant | None:
    """Grava/atualiza os dados vindos do scrape do HTML (fonte de verdade).

    Os campos em _HTML_LOCKED_FIELDS são sempre escritos com o valor mais recente do
    HTML (overwrite) — é assim que uma prorrogação que muda a closing_date no HTML é
    refletida na base de dados, mesmo que o PDF já tenha sido processado.
    """
    url = grant_dict.get("url") or grant_dict.get("scraping_url")
    grant_code = grant_dict.get("grant_code")
    if not url:
        return None

    # Match por scraping_url ou, em falha, por grant_code (re-scrape do mesmo aviso).
    grant = Grant.objects.filter(scraping_url=url).first()
    if grant is None and grant_code:
        grant = Grant.objects.filter(grant_code=grant_code).first()
    if grant is None:
        grant = Grant(scraping_url=url, source=source)

    changed = False
    if source and not grant.source:
        grant.source = source
        changed = True
    # grant_code/title: definidos uma vez (não mudam entre versões).
    if grant_code and not grant.grant_code:
        grant.grant_code = grant_code
        changed = True
    if grant_dict.get("title") and not grant.title:
        grant.title = grant_dict["title"]
        changed = True

    # Campos HTML autoritativos: overwrite sempre que o HTML traga valor não-vazio.
    for field in _HTML_LOCKED_FIELDS:
        value = grant_dict.get(field)
        if field == "total_allocation":
            value = _parse_allocation(value)
        if value in (None, "", []):
            continue
        if getattr(grant, field, None) != value:
            setattr(grant, field, value)
            changed = True

    if changed:
        grant.save()
    return grant


def save_ai_grant(
    dados_ia: dict,
    scraping_url: str = "",
    pdf_path: str = "",
    markdown_path: str = "",
    force_overwrite: bool = False,
) -> Grant | None:
    aviso_data = dados_ia.get("Grant", {})
    grant_code = aviso_data.get("grant_code")

    grant = None
    if scraping_url:
        grant = Grant.objects.filter(scraping_url=scraping_url).first()
    if grant is None and grant_code:
        grant = Grant.objects.filter(grant_code=grant_code).first()
    if grant is None:
        grant = Grant(scraping_url=scraping_url or f"unknown:{grant_code}", source="")

    # Aponta o scraping_url para o canónico mais recente (ex: nova republicação/alteração).
    if scraping_url and grant.scraping_url != scraping_url:
        grant.scraping_url = scraping_url

    for field in _GRANT_AI_FIELDS:
        value = aviso_data.get(field)
        if value in (None, [], ""):
            continue
        current_empty = getattr(grant, field, None) in (None, [], "")
        if field in _HTML_LOCKED_FIELDS:
            # Campos HTML autoritativos: a IA só preenche como fallback quando o HTML
            # não trouxe valor; nunca sobrescreve (mesmo com force_overwrite).
            if current_empty:
                setattr(grant, field, value)
        elif force_overwrite or current_empty:
            setattr(grant, field, value)

    if pdf_path:
        grant.pdf_path = pdf_path
    if markdown_path:
        grant.markdown_path = markdown_path
    grant.ai_processed = True
    grant.save()

    grant.beneficiaries_by_action.all().delete()
    grant.phases.all().delete()
    grant.covered_areas.all().delete()
    grant.phase_areas.all().delete()
    grant.financing_rates.all().delete()
    grant.expense_limits.all().delete()
    grant.non_compliance_penalties.all().delete()
    grant.evaluation_methodologies.all().delete()

    BeneficiaryByAction.objects.bulk_create([
        BeneficiaryByAction(
            grant=grant,
            action_type=b.get("action_type"),
            entities=b.get("entities", []),
        ) for b in dados_ia.get("BeneficiaryByAction", [])
    ])

    Phase.objects.bulk_create([
        Phase(
            grant=grant,
            phase_code=f.get("phase_code"),
            name=f.get("name"),
            start_date=f.get("start_date"),
            end_date=f.get("end_date"),
            access_condition=f.get("access_condition"),
        ) for f in dados_ia.get("phases", [])
    ])

    CoveredArea.objects.bulk_create([
        CoveredArea(
            grant=grant,
            area_code=a.get("area_code"),
            geographic_area=a.get("geographic_area"),
        ) for a in dados_ia.get("CoveredArea", [])
    ])

    PhaseArea.objects.bulk_create([
        PhaseArea(
            grant=grant,
            phase_code=fa.get("phase_code"),
            area_code=fa.get("area_code"),
            fund_name=fa.get("fund_name"),
            budget_allocation=fa.get("budget_allocation"),
            max_financing_rate=fa.get("max_financing_rate"),
        ) for fa in dados_ia.get("PhaseArea", [])
    ])

    FinancingRate.objects.bulk_create([
        FinancingRate(
            grant=grant,
            rate_code=t.get("rate_code"),
            company_size=t.get("company_size"),
            aid_regime=t.get("aid_regime"),
            base_rate=t.get("base_rate"),
            regional_bonus=t.get("regional_bonus"),
            max_global_rate=t.get("max_global_rate"),
            minimis_accumulation_limit=t.get("minimis_accumulation_limit"),
            specific_condition=t.get("specific_condition"),
        ) for t in dados_ia.get("FinancingRate", [])
    ])

    ExpenseLimit.objects.bulk_create([
        ExpenseLimit(
            grant=grant,
            limit_code=l.get("limit_code"),
            expense_category=l.get("expense_category"),
            applicable_ocs_methodology=l.get("applicable_ocs_methodology"),
            max_absolute_value=l.get("max_absolute_value"),
            max_percentage_value=l.get("max_percentage_value"),
            calculation_base=l.get("calculation_base"),
            specific_conditions=l.get("specific_conditions"),
        ) for l in dados_ia.get("ExpenseLimit", [])
    ])

    NonCompliancePenalty.objects.bulk_create([
        NonCompliancePenalty(
            grant=grant,
            penalty_code=p.get("penalty_code"),
            indicator_types=p.get("indicator_types"),
            general_tolerance_threshold=p.get("general_tolerance_threshold"),
            low_density_tolerance_threshold=p.get("low_density_tolerance_threshold"),
            reduction_per_percentage_point=p.get("reduction_per_percentage_point"),
            max_penalty_percentage=p.get("max_penalty_percentage"),
            financing_revocation_threshold=p.get("financing_revocation_threshold"),
            rule_description=p.get("rule_description"),
        ) for p in dados_ia.get("NonCompliancePenalty", [])
    ])

    EvaluationMethodology.objects.bulk_create([
        EvaluationMethodology(
            grant=grant,
            evaluation_code=m.get("evaluation_code"),
            project_merit_formula=m.get("project_merit_formula"),
            scoring_scale=m.get("scoring_scale"),
            min_global_score=m.get("min_global_score"),
            evaluation_criteria=m.get("evaluation_criteria", []),
            tiebreaker_criteria=m.get("tiebreaker_criteria", []),
        ) for m in dados_ia.get("EvaluationMethodology", [])
    ])

    return grant
