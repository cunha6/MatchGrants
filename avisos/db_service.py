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


def _find_or_build_grant(scraping_url: str, grant_code: str | None) -> Grant:
    """O aviso a que esta extração pertence: procura pelo URL canónico, depois pelo código;
    se não existir nenhum, constrói um novo (ainda por gravar).

    Sem `scraping_url` o aviso fica com um URL sintético `unknown:<código>` — é a chave
    natural do modelo e não pode ficar vazia.
    """
    grant = None
    if scraping_url:
        grant = Grant.objects.filter(scraping_url=scraping_url).first()
    if grant is None and grant_code:
        grant = Grant.objects.filter(grant_code=grant_code).first()
    if grant is None:
        return Grant(scraping_url=scraping_url or f"unknown:{grant_code}", source="")

    # Aponta o scraping_url para o canónico mais recente (ex: nova republicação/alteração).
    if scraping_url and grant.scraping_url != scraping_url:
        grant.scraping_url = scraping_url
    return grant


def _apply_ai_fields(grant: Grant, grant_fields: dict, force_overwrite: bool) -> None:
    """Escreve no aviso os campos extraídos pela IA.

    Três regras, por ordem de precedência:
    - valor vazio na extração nunca apaga o que lá está;
    - campo HTML autoritativo (_HTML_LOCKED_FIELDS): a IA só preenche o que o HTML deixou
      vazio, e nunca sobrescreve — nem com force_overwrite;
    - restantes: sobrescreve se `force_overwrite`, ou se o campo estiver vazio.
    """
    for field in _GRANT_AI_FIELDS:
        value = grant_fields.get(field)
        if value in (None, [], ""):
            continue
        current_empty = getattr(grant, field, None) in (None, [], "")
        if field in _HTML_LOCKED_FIELDS:
            if current_empty:
                setattr(grant, field, value)
        elif force_overwrite or current_empty:
            setattr(grant, field, value)


def _delete_children(grant: Grant) -> None:
    """Apaga as relações-filhas antes de as recriar.

    A ordem importa: `phases`/`covered_areas` são apagadas ANTES de `phase_areas`, para que a
    cascata das FKs não deixe linhas órfãs a meio.
    """
    grant.beneficiaries_by_action.all().delete()
    grant.phases.all().delete()
    grant.covered_areas.all().delete()
    grant.phase_areas.all().delete()
    grant.financing_rates.all().delete()
    grant.expense_limits.all().delete()
    grant.non_compliance_penalties.all().delete()
    grant.evaluation_methodologies.all().delete()


def _create_phases_and_areas(grant: Grant, ai_extraction: dict) -> tuple[dict, dict]:
    """Cria fases e áreas (uma a uma, para a BD gerar os ids) e devolve os mapas
    código-de-junção → instância, que ligam os PhaseArea por FK REAL."""
    phase_by_code: dict[str, Phase] = {}
    for phase_data in ai_extraction.get("phases", []):
        phase_record = Phase.objects.create(
            grant=grant,
            name=phase_data.get("name"),
            start_date=phase_data.get("start_date"),
            end_date=phase_data.get("end_date"),
            access_condition=phase_data.get("access_condition"),
        )
        join_code = phase_data.get("phase_code")
        if join_code:
            phase_by_code[str(join_code)] = phase_record

    area_by_code: dict[str, CoveredArea] = {}
    for area_data in ai_extraction.get("CoveredArea", []):
        area_record = CoveredArea.objects.create(
            grant=grant,
            geographic_area=area_data.get("geographic_area"),
        )
        join_code = area_data.get("area_code")
        if join_code:
            area_by_code[str(join_code)] = area_record
    return phase_by_code, area_by_code


def _create_children(grant: Grant, ai_extraction: dict) -> None:
    """Recria todas as relações-filhas a partir do JSON da IA."""
    BeneficiaryByAction.objects.bulk_create([
        BeneficiaryByAction(
            grant=grant,
            action_type=beneficiary_data.get("action_type"),
            entities=beneficiary_data.get("entities", []),
        ) for beneficiary_data in ai_extraction.get("BeneficiaryByAction", [])
    ])

    phase_by_code, area_by_code = _create_phases_and_areas(grant, ai_extraction)

    # phase/area ficam null quando o código não corresponde a nenhuma fase/área (ex: dotação
    # por fundo/global, cujo codigo_fase é "FEDER"/"GLOBAL").
    PhaseArea.objects.bulk_create([
        PhaseArea(
            grant=grant,
            phase=phase_by_code.get(str(phase_area_data.get("phase_code"))),
            area=area_by_code.get(str(phase_area_data.get("area_code"))),
            fund_name=phase_area_data.get("fund_name"),
            budget_allocation=phase_area_data.get("budget_allocation"),
            max_financing_rate=phase_area_data.get("max_financing_rate"),
            distribution=phase_area_data.get("distribution", []),
        ) for phase_area_data in ai_extraction.get("PhaseArea", [])
    ])

    FinancingRate.objects.bulk_create([
        FinancingRate(
            grant=grant,
            company_size=rate_data.get("company_size"),
            aid_regime=rate_data.get("aid_regime"),
            base_rate=rate_data.get("base_rate"),
            regional_bonus=rate_data.get("regional_bonus"),
            max_global_rate=rate_data.get("max_global_rate"),
            minimis_accumulation_limit=rate_data.get("minimis_accumulation_limit"),
            specific_condition=rate_data.get("specific_condition"),
        ) for rate_data in ai_extraction.get("FinancingRate", [])
    ])

    ExpenseLimit.objects.bulk_create([
        ExpenseLimit(
            grant=grant,
            expense_category=expense_limit_data.get("expense_category"),
            applicable_ocs_methodology=expense_limit_data.get("applicable_ocs_methodology"),
            max_absolute_value=expense_limit_data.get("max_absolute_value"),
            max_percentage_value=expense_limit_data.get("max_percentage_value"),
            calculation_base=expense_limit_data.get("calculation_base"),
            specific_conditions=expense_limit_data.get("specific_conditions"),
        ) for expense_limit_data in ai_extraction.get("ExpenseLimit", [])
    ])

    NonCompliancePenalty.objects.bulk_create([
        NonCompliancePenalty(
            grant=grant,
            indicator_types=penalty_data.get("indicator_types"),
            compliance_grade_formula=penalty_data.get("compliance_grade_formula"),
            general_tolerance_threshold=penalty_data.get("general_tolerance_threshold"),
            low_density_tolerance_threshold=penalty_data.get("low_density_tolerance_threshold"),
            reduction_per_percentage_point=penalty_data.get("reduction_per_percentage_point"),
            penalty_tiers=penalty_data.get("penalty_tiers", []),
            max_penalty_percentage=penalty_data.get("max_penalty_percentage"),
            financing_revocation_threshold=penalty_data.get("financing_revocation_threshold"),
            rule_description=penalty_data.get("rule_description"),
        ) for penalty_data in ai_extraction.get("NonCompliancePenalty", [])
    ])

    EvaluationMethodology.objects.bulk_create([
        EvaluationMethodology(
            grant=grant,
            project_merit_formula=methodology_data.get("project_merit_formula"),
            scoring_scale=methodology_data.get("scoring_scale"),
            min_global_score=methodology_data.get("min_global_score"),
            evaluation_criteria=methodology_data.get("evaluation_criteria", []),
            tiebreaker_criteria=methodology_data.get("tiebreaker_criteria", []),
        ) for methodology_data in ai_extraction.get("EvaluationMethodology", [])
    ])


def _log_ai_extraction(grant: Grant, ai_extraction: dict, markdown_path: str) -> None:
    """Regista o que a IA gerou (auditável em logs/avisos.log): campos preenchidos vs vazios
    e contagem das entidades relacionadas. O JSON completo fica em output/json/<fonte>.json."""
    filled_fields = [phase_data for phase_data in _GRANT_AI_FIELDS if getattr(grant, phase_data, None) not in (None, [], "")]
    empty_fields = [phase_data for phase_data in _GRANT_AI_FIELDS if phase_data not in filled_fields]
    logger.info(
        "IA GEROU aviso %s (id=%s, url=%s): %d/%d campos preenchidos | vazios: %s | "
        "fases=%d áreas=%d dotações=%d taxas=%d limites=%d penalizações=%d metodologias=%d "
        "beneficiários=%d | markdown=%s",
        grant.grant_code or "?", grant.pk, grant.scraping_url,
        len(filled_fields), len(_GRANT_AI_FIELDS), ", ".join(empty_fields) or "nenhum",
        len(ai_extraction.get("phases", [])), len(ai_extraction.get("CoveredArea", [])),
        len(ai_extraction.get("PhaseArea", [])), len(ai_extraction.get("FinancingRate", [])),
        len(ai_extraction.get("ExpenseLimit", [])), len(ai_extraction.get("NonCompliancePenalty", [])),
        len(ai_extraction.get("EvaluationMethodology", [])),
        len(ai_extraction.get("BeneficiaryByAction", [])),
        markdown_path or grant.markdown_path,
    )


def _save_embeddings(grant: Grant) -> None:
    """Pré-calcula os embeddings do aviso (um por tipo: GENERAL, SECTOR…), para o match
    semântico ficar SEMPRE pronto — o match só lê, nunca gera. Só chama a OpenAI para os
    tipos cujo texto mudou. Best-effort: sem OPENAI_API_KEY ou em falha, fica por gerar
    (o match cai para taxa+dotação) e a gravação do aviso segue na mesma."""
    # Import diferido: match.grant_embeddings importa avisos.models (evita import circular).
    try:
        from match.grant_embeddings import save_grant_embeddings
        save_grant_embeddings(grant)
    except Exception:
        logger.exception("Falha ao gerar os embeddings do aviso %s (segue sem semântica).",
                         grant.grant_code or grant.pk)


@transaction.atomic
def save_ai_grant(
    ai_extraction: dict,
    scraping_url: str = "",
    pdf_path: str = "",
    markdown_path: str = "",
    force_overwrite: bool = False,
) -> Grant | None:
    """Grava no aviso o resultado da extração da IA: campos + relações-filhas (substituídas
    por completo) + embeddings.

    Atómico: o delete+recreate das relações (fases, áreas, taxas…) nunca fica a meio — um
    erro faz rollback completo em vez de deixar o aviso sem filhos.
    """
    grant_fields = ai_extraction.get("Grant", {})
    grant = _find_or_build_grant(scraping_url, grant_fields.get("grant_code"))

    _apply_ai_fields(grant, grant_fields, force_overwrite)

    # annex_documents é uma chave de TOPO do JSON (não vem dentro de "Grant"), por isso não
    # entra em _apply_ai_fields. Vem fresca do scrape a cada processamento, logo atualiza-se
    # sempre que presente.
    annexes = ai_extraction.get("annex_documents")
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

    _delete_children(grant)
    _create_children(grant, ai_extraction)
    _log_ai_extraction(grant, ai_extraction, markdown_path)
    _save_embeddings(grant)

    return grant
