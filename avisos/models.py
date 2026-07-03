from django.db import models


class Grant(models.Model):
    SOURCE_CHOICES = [
        ("compete", "Compete2030"),
        ("portugal", "Portugal2030"),
        ("prr", "PRR"),
    ]

    # Scraping metadata
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, db_index=True)
    scraping_url = models.URLField(max_length=500, unique=True)
    pdf_path = models.CharField(max_length=500, blank=True)
    markdown_path = models.CharField(max_length=500, blank=True)
    ai_processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # P1 — identificação
    grant_code = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    title = models.TextField(blank=True, null=True)
    financing_program = models.TextField(blank=True, null=True)
    managing_entity = models.TextField(blank=True, null=True)
    publication_date = models.TextField(blank=True, null=True)
    opening_date = models.TextField(blank=True, null=True)
    closing_date = models.TextField(blank=True, null=True)
    republication_date = models.TextField(blank=True, null=True)
    last_republication = models.TextField(blank=True, null=True)
    amendment_date = models.TextField(blank=True, null=True)
    notice_modality = models.CharField(max_length=255, blank=True, null=True)
    objective = models.TextField(blank=True, null=True)
    fund_name = models.CharField(max_length=255, blank=True, null=True)
    program_priority = models.TextField(blank=True, null=True)
    intervention_type_code = models.TextField(blank=True, null=True)
    max_duration_months = models.IntegerField(null=True, blank=True)
    # Padrões CAE com wildcard ('*') normalizados pela IA, hierárquicos por prefixo:
    # Divisão '64***', Grupo '651**', Classe '6512*', Subclasse '65124'.
    # included_caes: se não vazio, SÓ estes são elegíveis. excluded_caes: tudo menos estes.
    # Ambos vazios ⇒ sem restrição de CAE (qualquer CAE elegível).
    included_caes = models.JSONField(default=list, blank=True)
    excluded_caes = models.JSONField(default=list, blank=True)
    eligible_regions = models.JSONField(default=list, blank=True)
    expense_eligibility_start_date = models.TextField(blank=True, null=True)
    specific_objective = models.TextField(blank=True, null=True)
    operation_typology = models.TextField(blank=True, null=True)
    covered_actions = models.TextField(blank=True, null=True)
    intermediate_bodies = models.JSONField(default=list, blank=True)
    applicable_legislation = models.JSONField(default=list, blank=True)
    regulatory_documents = models.JSONField(default=list, blank=True)
    target_technology_sectors = models.JSONField(default=list, blank=True)
    application_submission = models.TextField(blank=True, null=True)
    beneficiary_eligibility_criteria = models.JSONField(default=list, blank=True)
    # QUE operações são elegíveis (distinto de QUEM se pode candidatar, acima).
    operation_eligibility_criteria = models.JSONField(default=list, blank=True)
    admissibility_conditions = models.JSONField(default=list, blank=True)
    final_recipients = models.JSONField(default=list, blank=True)
    dnsh_principle = models.TextField(blank=True, null=True)
    commitment_requirements = models.TextField(blank=True, null=True)
    # P2 — território e execução
    total_allocation = models.FloatField(null=True, blank=True)
    low_density_territories = models.JSONField(default=list, blank=True)
    submission_limits = models.TextField(blank=True, null=True)
    absolute_execution_deadline = models.TextField(blank=True, null=True)
    financial_execution_targets = models.JSONField(default=list, blank=True)
    # P3 — financeiro
    minimum_investment = models.FloatField(null=True, blank=True)
    maximum_investment = models.FloatField(null=True, blank=True)
    required_self_financing_limit = models.FloatField(null=True, blank=True)
    state_aid_regime = models.TextField(blank=True, null=True)
    applicable_gber_article = models.TextField(blank=True, null=True)
    contact = models.JSONField(default=list, blank=True)
    payment_methods = models.JSONField(default=list, blank=True)
    # P4 — avaliação
    project_selection_criteria = models.JSONField(default=list, blank=True)
    # P5 — despesas e indicadores
    eligible_expenses = models.JSONField(default=list, blank=True)
    ineligible_expenses = models.JSONField(default=list, blank=True)
    output_indicators = models.JSONField(default=list, blank=True)
    result_indicators = models.JSONField(default=list, blank=True)
    monitoring_indicators = models.JSONField(default=list, blank=True)
    beneficiary_obligations = models.JSONField(default=list, blank=True)
    communication_obligations = models.JSONField(default=list, blank=True)
    # P6 — documentos
    application_documents = models.JSONField(default=list, blank=True)
    # P7 — annex topics not captured (for future review)
    to_explore = models.JSONField(default=list, blank=True)
    # Campos adicionais (cobertura)
    bonus_mechanisms = models.JSONField(default=list, blank=True)
    dnsh_criteria = models.TextField(blank=True, null=True)
    # Marcado quando há alterações não consolidadas que precisam de revisão manual
    needs_review = models.BooleanField(default=False)

    def __str__(self):
        return self.grant_code or self.title or f"Grant #{self.pk}"


class BeneficiaryByAction(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="beneficiaries_by_action")
    action_type = models.TextField(blank=True, null=True)
    entities = models.JSONField(default=list, blank=True)


class Phase(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="phases")
    phase_code = models.CharField(max_length=255, blank=True, null=True)
    name = models.CharField(max_length=255, blank=True, null=True)
    start_date = models.TextField(blank=True, null=True)
    end_date = models.TextField(blank=True, null=True)
    access_condition = models.TextField(blank=True, null=True)


class CoveredArea(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="covered_areas")
    area_code = models.CharField(max_length=255, blank=True, null=True)
    geographic_area = models.TextField(blank=True, null=True)


class PhaseArea(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="phase_areas")
    phase_code = models.CharField(max_length=255, blank=True, null=True)
    area_code = models.CharField(max_length=255, blank=True, null=True)
    fund_name = models.CharField(max_length=255, blank=True, null=True)
    budget_allocation = models.FloatField(null=True, blank=True)
    max_financing_rate = models.FloatField(null=True, blank=True)


class FinancingRate(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="financing_rates")
    rate_code = models.CharField(max_length=255, blank=True, null=True)
    company_size = models.TextField(blank=True, null=True)
    aid_regime = models.TextField(blank=True, null=True)
    base_rate = models.TextField(blank=True, null=True)
    regional_bonus = models.TextField(blank=True, null=True)
    max_global_rate = models.TextField(blank=True, null=True)
    minimis_accumulation_limit = models.FloatField(null=True, blank=True)
    specific_condition = models.TextField(blank=True, null=True)


class ExpenseLimit(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="expense_limits")
    limit_code = models.CharField(max_length=255, blank=True, null=True)
    expense_category = models.TextField(blank=True, null=True)
    applicable_ocs_methodology = models.TextField(blank=True, null=True)
    max_absolute_value = models.FloatField(null=True, blank=True)
    max_percentage_value = models.FloatField(null=True, blank=True)
    calculation_base = models.TextField(blank=True, null=True)
    specific_conditions = models.TextField(blank=True, null=True)


class NonCompliancePenalty(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="non_compliance_penalties")
    penalty_code = models.CharField(max_length=255, blank=True, null=True)
    indicator_types = models.TextField(blank=True, null=True)
    compliance_grade_formula = models.TextField(blank=True, null=True)
    general_tolerance_threshold = models.FloatField(null=True, blank=True)
    low_density_tolerance_threshold = models.FloatField(null=True, blank=True)
    reduction_per_percentage_point = models.FloatField(null=True, blank=True)
    max_penalty_percentage = models.FloatField(null=True, blank=True)
    financing_revocation_threshold = models.FloatField(null=True, blank=True)
    rule_description = models.TextField(blank=True, null=True)


class EvaluationMethodology(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="evaluation_methodologies")
    evaluation_code = models.CharField(max_length=255, blank=True, null=True)
    project_merit_formula = models.TextField(blank=True, null=True)
    scoring_scale = models.TextField(blank=True, null=True)
    min_global_score = models.FloatField(null=True, blank=True)
    evaluation_criteria = models.JSONField(default=list, blank=True)
    tiebreaker_criteria = models.JSONField(default=list, blank=True)


class GrantDocument(models.Model):
    """Cada documento de um aviso: base, republicação, alteração, prorrogação, anexo."""
    DOC_TYPES = [
        ("base", "Base"),
        ("republication", "Republicação"),
        ("amendment", "Alteração"),
        ("prorrogation", "Prorrogação"),
        ("rectification", "Retificação"),
        ("annex", "Anexo"),
        ("other", "Outro"),
    ]
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=20, choices=DOC_TYPES, default="other", db_index=True)
    name = models.TextField(blank=True, null=True)
    url = models.URLField(max_length=500, blank=True)
    local_path = models.CharField(max_length=500, blank=True)
    ordinal = models.IntegerField(default=0)
    is_canonical = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.doc_type}: {self.name or self.url}"
