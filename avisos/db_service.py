import logging
import re

from django.db import transaction

from .models import (
    Grant, BeneficiaryByAction, Phase, CoveredArea,
    PhaseArea, FinancingRate, ExpenseLimit, NonCompliancePenalty,
    EvaluationMethodology,
)

logger = logging.getLogger(__name__)  # 'avisos.db_service' → consola + logs/avisos.log

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
    "maximum_self_financing", "state_aid_regime", "applicable_gber_article",
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
        # A última escrita foi o scrape do HTML — mesmo que o aviso já tivesse sido editado à
        # mão antes, a origem passa a refletir esta escrita mais recente.
        grant.last_update_source = Grant.SOURCE_SCRAPE
        grant.last_updated_by = None
        grant.save()
    return grant


@transaction.atomic
def save_ai_grant(
    dados_ia: dict,
    scraping_url: str = "",
    pdf_path: str = "",
    markdown_path: str = "",
    force_overwrite: bool = False,
) -> Grant | None:
    # Atómico: o delete+recreate das relações (fases, áreas, taxas…) nunca fica a meio —
    # um erro faz rollback completo em vez de deixar o aviso sem filhos.
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

    # annex_documents é uma chave de TOPO do JSON (não vem dentro de "Grant"), por isso não
    # entra no loop _GRANT_AI_FIELDS acima — tratamo-la aqui. Vem fresca do scrape a cada
    # processamento, logo atualiza-se quando presente.
    annexes = dados_ia.get("annex_documents")
    if annexes is not None:
        grant.annex_documents = annexes or []

    if pdf_path:
        grant.pdf_path = pdf_path
    if markdown_path:
        grant.markdown_path = markdown_path
    grant.ai_processed = True
    # A última escrita foi a extração IA do pipeline — não um humano.
    grant.last_update_source = Grant.SOURCE_SCRAPE
    grant.last_updated_by = None
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

    # Fases e áreas: cria os registos (a BD gera os ids) e mapeia o código de junção do
    # JSON (codigo_fase/codigo_area) → instância, para ligar o PhaseArea por FK REAL.
    phase_by_code: dict[str, Phase] = {}
    for f in dados_ia.get("phases", []):
        ph = Phase.objects.create(
            grant=grant,
            name=f.get("name"),
            start_date=f.get("start_date"),
            end_date=f.get("end_date"),
            access_condition=f.get("access_condition"),
        )
        code = f.get("phase_code")
        if code:
            phase_by_code[str(code)] = ph

    area_by_code: dict[str, CoveredArea] = {}
    for a in dados_ia.get("CoveredArea", []):
        ar = CoveredArea.objects.create(
            grant=grant,
            geographic_area=a.get("geographic_area"),
        )
        code = a.get("area_code")
        if code:
            area_by_code[str(code)] = ar

    # PhaseArea liga-se a Phase/CoveredArea por FK (via os códigos de junção). phase/area
    # ficam null quando o código não corresponde a nenhuma fase/área (ex: dotação por
    # fundo/global, cujo codigo_fase é "FEDER"/"GLOBAL").
    PhaseArea.objects.bulk_create([
        PhaseArea(
            grant=grant,
            phase=phase_by_code.get(str(fa.get("phase_code"))),
            area=area_by_code.get(str(fa.get("area_code"))),
            fund_name=fa.get("fund_name"),
            budget_allocation=fa.get("budget_allocation"),
            max_financing_rate=fa.get("max_financing_rate"),
            distribution=fa.get("distribution", []),
        ) for fa in dados_ia.get("PhaseArea", [])
    ])

    FinancingRate.objects.bulk_create([
        FinancingRate(
            grant=grant,
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
            expense_category=lim.get("expense_category"),
            applicable_ocs_methodology=lim.get("applicable_ocs_methodology"),
            max_absolute_value=lim.get("max_absolute_value"),
            max_percentage_value=lim.get("max_percentage_value"),
            calculation_base=lim.get("calculation_base"),
            specific_conditions=lim.get("specific_conditions"),
        ) for lim in dados_ia.get("ExpenseLimit", [])
    ])

    NonCompliancePenalty.objects.bulk_create([
        NonCompliancePenalty(
            grant=grant,
            indicator_types=p.get("indicator_types"),
            compliance_grade_formula=p.get("compliance_grade_formula"),
            general_tolerance_threshold=p.get("general_tolerance_threshold"),
            low_density_tolerance_threshold=p.get("low_density_tolerance_threshold"),
            reduction_per_percentage_point=p.get("reduction_per_percentage_point"),
            penalty_tiers=p.get("penalty_tiers", []),
            max_penalty_percentage=p.get("max_penalty_percentage"),
            financing_revocation_threshold=p.get("financing_revocation_threshold"),
            rule_description=p.get("rule_description"),
        ) for p in dados_ia.get("NonCompliancePenalty", [])
    ])

    EvaluationMethodology.objects.bulk_create([
        EvaluationMethodology(
            grant=grant,
            project_merit_formula=m.get("project_merit_formula"),
            scoring_scale=m.get("scoring_scale"),
            min_global_score=m.get("min_global_score"),
            evaluation_criteria=m.get("evaluation_criteria", []),
            tiebreaker_criteria=m.get("tiebreaker_criteria", []),
        ) for m in dados_ia.get("EvaluationMethodology", [])
    ])

    # Registo do que a IA gerou (auditável em logs/avisos.log): campos preenchidos vs vazios
    # e contagem das entidades relacionadas. O JSON completo fica em output/json/<fonte>.json.
    filled = [f for f in _GRANT_AI_FIELDS if getattr(grant, f, None) not in (None, [], "")]
    empty = [f for f in _GRANT_AI_FIELDS if f not in filled]
    logger.info(
        "IA GEROU aviso %s (id=%s, url=%s): %d/%d campos preenchidos | vazios: %s | "
        "fases=%d áreas=%d dotações=%d taxas=%d limites=%d penalizações=%d metodologias=%d "
        "beneficiários=%d | markdown=%s",
        grant.grant_code or "?", grant.pk, grant.scraping_url,
        len(filled), len(_GRANT_AI_FIELDS), ", ".join(empty) or "nenhum",
        len(dados_ia.get("phases", [])), len(dados_ia.get("CoveredArea", [])),
        len(dados_ia.get("PhaseArea", [])), len(dados_ia.get("FinancingRate", [])),
        len(dados_ia.get("ExpenseLimit", [])), len(dados_ia.get("NonCompliancePenalty", [])),
        len(dados_ia.get("EvaluationMethodology", [])),
        len(dados_ia.get("BeneficiaryByAction", [])),
        markdown_path or grant.markdown_path,
    )

    # Pré-calcula os embeddings do aviso (um por tipo: GENERAL, SECTOR…), para o match
    # semântico ficar SEMPRE pronto — o match só lê, nunca gera. Só chama a OpenAI para os
    # tipos cujo texto mudou. Best-effort: sem OPENAI_API_KEY ou em falha, fica por gerar
    # (o match cai para taxa+dotação) e a gravação do aviso segue na mesma.
    # Import diferido: match.grant_embeddings importa avisos.models (evita import circular).
    try:
        from match.grant_embeddings import save_grant_embeddings
        save_grant_embeddings(grant)
    except Exception:
        logger.exception("Falha ao gerar os embeddings do aviso %s (segue sem semântica).",
                         grant.grant_code or grant.pk)

    return grant
