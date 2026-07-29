from django.conf import settings
from django.db import models
from django.db.models import Q
from pgvector.django import HnswIndex, VectorField

# Dimensão dos embeddings OpenAI (text-embedding-3-small).
EMBEDDING_DIM = 1536


class Grant(models.Model):
    SOURCE_CHOICES = [
        ("compete", "Compete2030"),
        ("portugal", "Portugal2030"),
        ("prr", "PRR"),
    ]

    # Scraping metadata
    source = models.CharField(max_length=100, choices=SOURCE_CHOICES, db_index=True)
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
    notice_modality = models.CharField(max_length=500, blank=True, null=True)
    objective = models.TextField(blank=True, null=True)
    fund_name = models.TextField(blank=True, null=True)  # multi-fundo (" + ") pode passar 255
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
    # Todos os documentos da página do aviso (nome + url) — referência; não são descarregados.
    annex_documents = models.JSONField(default=list, blank=True)
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
    # OVERRIDE MANUAL da taxa de financiamento mostrada. Por defeito null = a taxa é CALCULADA
    # das linhas-filhas (PhaseArea/FinancingRate). Se preenchida (via edição), prevalece sobre
    # o cálculo — permite corrigir/fixar a taxa à mão. Voltar a null repõe o cálculo automático.
    financing_rate = models.FloatField(null=True, blank=True)
    low_density_territories = models.JSONField(default=list, blank=True)
    submission_limits = models.TextField(blank=True, null=True)
    absolute_execution_deadline = models.TextField(blank=True, null=True)
    financial_execution_targets = models.JSONField(default=list, blank=True)
    # P3 — financeiro
    minimum_investment = models.FloatField(null=True, blank=True)
    maximum_investment = models.FloatField(null=True, blank=True)
    # Percentagem/valor MÁXIMO de autofinanciamento (capitais próprios) do promotor.
    maximum_self_financing = models.FloatField(null=True, blank=True)
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
    # Ativo = ainda a decorrer. Fica False quando a closing_date já passou (avisos terminados
    # são escondidos do match, mas os ficheiros PDF/markdown/JSON são MANTIDOS).
    active = models.BooleanField(default=True, db_index=True)

    # Origem da ÚLTIMA escrita neste aviso: 'scrape' (pipeline de scrape/IA, save_scraped_grant/
    # save_ai_grant) ou 'manual' (PUT/PATCH em /avisos/<id>/edit/). Distingue um aviso tal como o
    # scrape o deixou de um que um admin/commercial já corrigiu à mão — útil para saber que avisos
    # NÃO reverter/sobrescrever sem verificar, e para auditoria (quem editou fica em `last_updated_by`).
    SOURCE_SCRAPE = "scrape"
    SOURCE_MANUAL = "manual"
    LAST_UPDATE_SOURCE_CHOICES = [(SOURCE_SCRAPE, "Scrape/IA"), (SOURCE_MANUAL, "Manual")]
    last_update_source = models.CharField(
        max_length=10, choices=LAST_UPDATE_SOURCE_CHOICES, default=SOURCE_SCRAPE, db_index=True,
    )
    # Utilizador que fez a última edição MANUAL; None quando a última escrita foi do scrape/IA.
    last_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    # DEPRECADO — substituído pelos embeddings especializados em `GrantEmbedding` (um por
    # tipo: GENERAL, SECTOR, …). Mantidos por retrocompatibilidade/rollback; já não são
    # escritos nem lidos. Podem ser removidos numa migração futura de limpeza.
    activity_embedding = VectorField(dimensions=EMBEDDING_DIM, null=True, blank=True)
    activity_embedding_hash = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        indexes = [
            # Índice PARCIAL: só contém as linhas do CONJUNTO QUENTE (avisos processados e
            # ativos) — que o match filtra em cada pedido. Como só indexa esses ~1-2k, a query
            # `WHERE ai_processed AND active` fica instantânea mesmo com dezenas de milhares de
            # avisos históricos (inativos) na tabela. É o único ponto onde o histórico tocava o
            # match; este índice resolve-o sem qualquer redesenho.
            models.Index(
                fields=["id"], name="grant_active_processed_idx",
                condition=Q(ai_processed=True, active=True),
            ),
        ]

    def __str__(self):
        return self.grant_code or self.title or f"Grant #{self.pk}"


class GrantEmbedding(models.Model):
    """Embedding ESPECIALIZADO de um aviso — um registo por TIPO (GENERAL, SECTOR, …).

    Porquê vários em vez de um só: cada tipo isola uma dimensão do aviso e é comparado de
    forma independente com a dimensão equivalente da empresa. Num embedding único, o sinal
    setorial ("valorização de resíduos") dilui-se no meio do texto geral (objetivos, ações,
    regiões) e deixa de discriminar; separado, pesa por si.

    Extensibilidade: acrescentar um tipo (RECIPIENT, OBJECTIVE, …) NÃO altera a estrutura da
    base de dados — basta juntar o valor a `Type`, escrever o builder do texto e registá-lo
    em `match/grant_embeddings.py::_TEXT_BUILDERS`. Os dados entram como linhas novas.
    """

    class Type(models.TextChoices):
        GENERAL = "GENERAL", "Geral (conteúdo do aviso)"
        SECTOR = "SECTOR", "Setorial (domínio tecnológico/económico)"

    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="embeddings")
    embedding_type = models.CharField(max_length=32, choices=Type.choices, db_index=True)
    # Modelo que gerou o vetor (ex: "text-embedding-3-small") — permite detetar/migrar
    # embeddings gerados por um modelo antigo sem os confundir com os atuais.
    model = models.CharField(max_length=100)
    embedding = VectorField(dimensions=EMBEDDING_DIM)
    # Hash do texto embebido: é o que permite NÃO chamar a OpenAI quando o texto não mudou.
    text_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # "Um embedding por tipo": o (re)cálculo é sempre um update_or_create desta chave.
            models.UniqueConstraint(
                fields=["grant", "embedding_type"], name="uniq_grant_embedding_per_type",
            ),
        ]
        indexes = [
            # HNSW para pesquisa vetorial por distância de cosseno no Postgres.
            HnswIndex(
                name="grant_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self):
        return f"{self.grant_id} · {self.embedding_type}"


class GrantCae(models.Model):
    """Índice NORMALIZADO dos padrões CAE de um aviso — uma linha por padrão (prefixo + tipo).

    Porquê uma tabela e não só o JSONField `included_caes`/`excluded_caes`: para o match poder
    PREFILTRAR no SQL (com índice) quais os avisos CAE-candidatos a um cliente, em vez de
    carregar todos os avisos e filtrar em Python. A `Grant.included_caes`/`excluded_caes`
    continua a ser a FONTE DE VERDADE (é o que a extração escreve e a edição altera); esta
    tabela é derivada dela e mantida em sincronia por signal (ver avisos/signals.py).

    A regra fina — "o prefixo mais específico ganha" entre inclusões e exclusões — continua a
    ser resolvida no Python (match_cae). Esta tabela só serve para o Postgres NARROWed o
    conjunto: devolve os avisos sem lista de inclusão OU cuja inclusão bate num prefixo do
    cliente (é impossível ser CAE-elegível fora desse conjunto).
    """
    INCLUDED = "included"
    EXCLUDED = "excluded"
    KIND_CHOICES = [(INCLUDED, INCLUDED), (EXCLUDED, EXCLUDED)]

    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="cae_entries")
    prefix = models.CharField(max_length=5)          # prefixo numérico do padrão ('64', '651', '65124')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)

    class Meta:
        indexes = [models.Index(fields=["prefix", "kind"])]

    def __str__(self):
        return f"{self.grant_id} · {self.kind} {self.prefix}"


class BeneficiaryByAction(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="beneficiaries_by_action")
    action_type = models.TextField(blank=True, null=True)
    entities = models.JSONField(default=list, blank=True)


class Phase(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="phases")
    name = models.TextField(blank=True, null=True)
    start_date = models.TextField(blank=True, null=True)
    end_date = models.TextField(blank=True, null=True)
    access_condition = models.TextField(blank=True, null=True)


class CoveredArea(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="covered_areas")
    geographic_area = models.TextField(blank=True, null=True)


class PhaseArea(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="phase_areas")
    # Ligações REAIS (a BD gera os ids; sem códigos inventados). `phase`/`area` ficam null
    # quando a dotação não é por fase (ex: dotação por fundo/global) ou não tem área específica.
    phase = models.ForeignKey(Phase, on_delete=models.CASCADE, related_name="phase_areas",
                              null=True, blank=True)
    area = models.ForeignKey(CoveredArea, on_delete=models.CASCADE, related_name="phase_areas",
                             null=True, blank=True)
    fund_name = models.TextField(blank=True, null=True)
    budget_allocation = models.FloatField(null=True, blank=True)
    max_financing_rate = models.FloatField(null=True, blank=True)
    # Repartição da dotação desta fase/área por território (ex: Baixa Densidade vs Outros).
    # Lista de {"name": str, "budget": float}. Ex: [{"name":"Baixa Densidade","budget":40000000.0},
    # {"name":"Outros Territórios","budget":60000000.0}].
    distribution = models.JSONField(default=list, blank=True)


class FinancingRate(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="financing_rates")
    company_size = models.TextField(blank=True, null=True)
    aid_regime = models.TextField(blank=True, null=True)
    base_rate = models.TextField(blank=True, null=True)
    regional_bonus = models.TextField(blank=True, null=True)
    max_global_rate = models.TextField(blank=True, null=True)
    minimis_accumulation_limit = models.FloatField(null=True, blank=True)
    specific_condition = models.TextField(blank=True, null=True)


class ExpenseLimit(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="expense_limits")
    expense_category = models.TextField(blank=True, null=True)
    applicable_ocs_methodology = models.TextField(blank=True, null=True)
    max_absolute_value = models.FloatField(null=True, blank=True)
    max_percentage_value = models.FloatField(null=True, blank=True)
    calculation_base = models.TextField(blank=True, null=True)
    specific_conditions = models.TextField(blank=True, null=True)


class NonCompliancePenalty(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="non_compliance_penalties")
    indicator_types = models.TextField(blank=True, null=True)
    compliance_grade_formula = models.TextField(blank=True, null=True)
    general_tolerance_threshold = models.FloatField(null=True, blank=True)
    low_density_tolerance_threshold = models.FloatField(null=True, blank=True)
    reduction_per_percentage_point = models.FloatField(null=True, blank=True)
    # Escalões discretizados da penalização: cada faixa de Grau de Cumprimento com a sua
    # redução em p.p. Ex: [{"grade_range": "] 70% - 65% ]", "reduction_pp": 0.5}, ...].
    penalty_tiers = models.JSONField(default=list, blank=True)
    max_penalty_percentage = models.FloatField(null=True, blank=True)
    financing_revocation_threshold = models.FloatField(null=True, blank=True)
    rule_description = models.TextField(blank=True, null=True)


class EvaluationMethodology(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE, related_name="evaluation_methodologies")
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
    doc_type = models.CharField(max_length=200, choices=DOC_TYPES, default="other", db_index=True)
    name = models.TextField(blank=True, null=True)
    url = models.URLField(max_length=500, blank=True)
    local_path = models.CharField(max_length=500, blank=True)
    ordinal = models.IntegerField(default=0)
    is_canonical = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.doc_type}: {self.name or self.url}"
