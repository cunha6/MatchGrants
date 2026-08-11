from django.conf import settings
from django.db import models
from pgvector.django import VectorField

# Dimensão dos embeddings OpenAI (text-embedding-3-small).
EMBEDDING_DIM = 1536


class Notice(models.Model):
    """Public procurement notice imported from the base.gov.pt API.

    Only notices whose `description` contains at least one keyword of interest are
    stored (see anuncios/services.py). Most external-data fields are optional to
    tolerate incomplete API responses.
    """

    # Options for `act_type`
    class ActTypeChoices(models.TextChoices):
        PROCEDURE = 'Anúncio de procedimento', 'Anúncio de procedimento'
        URGENT = 'Anúncio de concurso urgente', 'Anúncio de concurso urgente'
        RECTIFICATION = 'Declaração de retificação de anúncio', 'Declaração de retificação de anúncio'
        EXTENSION = 'Aviso de prorrogação de prazo', 'Aviso de prorrogação de prazo'
        AMENDMENT = 'Anúncio de Alteração', 'Anúncio de Alteração'

    # Options for `procedure_type`
    class ProcedureTypeChoices(models.TextChoices):
        PUBLIC = 'Concurso público', 'Concurso público'
        PUBLIC_URGENT = 'Concurso público urgente', 'Concurso público urgente'
        RESTRICTED = 'Concurso limitado por prévia qualificação', 'Concurso limitado por prévia qualificação'
        NEGOTIATION = 'Procedimento de negociação', 'Procedimento de negociação'
        COMPETITIVE_DIALOGUE = 'Diálogo concorrencial', 'Diálogo concorrencial'
        DESIGN_CONTEST = 'Concurso de concepção', 'Concurso de concepção'
        SIMPLIFIED_NOTICE = 'Anúncio simplificado', 'Anúncio simplificado'
        QUALIFICATION_SYSTEM = 'Instituição de sistema de qualificação', 'Instituição de sistema de qualificação'
        CONCESSIONAIRES_INTENT = (
            'Intenção de celebração de empreitadas de obras publicas por concessionários que não sejam entidades adjudicantes',
            'Intenção de celebração de empreitadas de obras publicas por concessionários que não sejam entidades adjudicantes'
        )
        INNOVATION_PARTNERSHIP = 'Parceria para a inovação', 'Parceria para a inovação'
        IDEAS_CONTEST = 'Concurso de ideias', 'Concurso de ideias'
        DYNAMIC_PURCHASING = 'Instituição de sistema de aquisição dinâmico', 'Instituição de sistema de aquisição dinâmico'
        PUBLIC_AUCTION = 'Hasta Pública de Alienação de Bens Móveis', 'Hasta Pública de Alienação de Bens Móveis'
        SOCIAL_SERVICES = 'Aquisição de Serviços Sociais e de Outros Serviços Específicos', 'Aquisição de Serviços Sociais e de Outros Serviços Específicos'
        SOCIAL_SERVICES_AWARD = 'Anúncio de Adjudicação de Aquisição de Serviços Sociais e de Outros Serviços Específicos', 'Anúncio de Adjudicação de Aquisição de Serviços Sociais e de Outros Serviços Específicos'
        PUBLIC_SIMPLIFIED = 'Concurso público simplificado', 'Concurso público simplificado'
        RESTRICTED_SIMPLIFIED = 'Concurso limitado por prévia qualificação simplificado', 'Concurso limitado por prévia qualificação simplificado'

    # Natural key for upsert: the official/canonical unique identifier of each notice.
    notice_number = models.CharField(max_length=20, unique=True, verbose_name="Notice Number")

    # Internal INCM identifier (informational).
    incm_id = models.CharField(max_length=20, blank=True, default="", verbose_name="INCM ID")
    publication_date = models.DateField(null=True, blank=True, verbose_name="Publication Date")
    entity_nif = models.CharField(max_length=9, blank=True, verbose_name="Entity Tax ID")
    entity_name = models.CharField(max_length=255, blank=True, verbose_name="Entity Name")
    description = models.TextField(blank=True, verbose_name="Description")
    url = models.URLField(max_length=500, blank=True, verbose_name="Notice URL")
    dr_number = models.CharField(max_length=10, blank=True, verbose_name="DR Number")
    series = models.CharField(max_length=5, blank=True, verbose_name="Series")

    act_type = models.CharField(
        max_length=100,
        choices=ActTypeChoices.choices,
        blank=True,
        verbose_name="Act Type",
    )

    # Multi-value fields stored as JSON arrays.
    contract_types = models.JSONField(default=list, blank=True, verbose_name="Contract Types")
    cpvs = models.JSONField(default=list, blank=True, verbose_name="CPVs")
    lots = models.JSONField(default=list, blank=True, verbose_name="Lots")

    base_price = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True, verbose_name="Base Price",
    )

    procedure_type = models.CharField(
        max_length=255,
        choices=ProcedureTypeChoices.choices,
        blank=True,
        verbose_name="Procedure Type",
    )

    year = models.IntegerField(null=True, blank=True, verbose_name="Year")
    environmental_criteria = models.BooleanField(default=False, verbose_name="Environmental Criteria")
    proposal_period_days = models.IntegerField(null=True, blank=True, verbose_name="Proposal Period (days)")
    procedure_documents_url = models.URLField(max_length=500, blank=True, verbose_name="Procedure Documents (URL)")

    # Local path (relative to the project root, under pdf_Anuncios/) of the tender
    # specifications ("Caderno de Encargos") downloaded from the procedure documents.
    # Empty if not found — the admin can fill/edit/clear it manually.
    specifications_path = models.CharField(
        max_length=500, blank=True, default="",
        verbose_name="Tender Specifications (local path)",
        help_text="Local path (under pdf_Anuncios/) of the tender specifications PDF. "
                  "Can be added/edited/removed manually.",
    )

    # Local path do "Programa de Concurso" (Programa do Procedimento / PC / PP) — o outro
    # documento base do procedimento, descarregado junto do caderno de encargos. Mesma
    # semântica de specifications_path: vazio se não encontrado; editável à mão.
    program_path = models.CharField(
        max_length=500, blank=True, default="",
        verbose_name="Competition Program (local path)",
        help_text="Local path (under pdf_Anuncios/) of the competition program "
                  "(Programa de Concurso) PDF. Can be added/edited/removed manually.",
    )

    # Local path (relative a BASE_DIR, sob output/markdown/) do caderno de encargos já
    # convertido para markdown. Preenchido/atualizado por specifications_ai.py na primeira
    # vez que /anuncios/<id>/detail/ é chamado — evita reconverter o PDF em cada pedido.
    # Vazio se ainda não foi convertido ou se não há specifications_path.
    specifications_markdown_path = models.CharField(
        max_length=500, blank=True, default="",
        verbose_name="Tender Specifications (markdown path)",
        help_text="Local path (under output/markdown/) of the tender specifications "
                  "converted to markdown. Generated on first AI-detail request.",
    )

    # Resposta da IA sobre o caderno de encargos — cache do resultado de
    # specifications_ai.generate_detail, para não repetir a chamada (paga) ao OpenAI em
    # cada pedido a /anuncios/<id>/detail/. Três campos em vez de um único JSON opaco:
    # descrição e avaliação são texto simples; observações fica em JSON por ser
    # estruturalmente uma lista de notas avulsas.
    specifications_description = models.TextField(
        blank=True, default="", verbose_name="Tender Specifications (AI description)",
        help_text="Cached AI-generated detailed description of the tender's object/scope.",
    )
    # JSONField por historial (guardava {criterios, formula}); hoje guarda só uma frase —
    # ver a regra de "avaliacao" em anuncios/specifications_ai.py:SYSTEM_PROMPT. Um
    # JSONField aceita uma string perfeitamente bem, por isso não houve migração — só
    # specifications_ai._coerce_avaliacao tem de lidar com registos antigos no formato
    # objeto (regenerados com ?refresh=true ficam já no formato novo).
    specifications_evaluation = models.JSONField(
        default=str, blank=True, verbose_name="Tender Specifications (AI evaluation)",
        help_text="Cached AI-generated evaluation-criteria summary (single sentence), "
                  "as written in the document.",
    )
    # [str, ...] — uma nota por item (ex: "Visita ao local obrigatória.").
    specifications_observations = models.JSONField(
        default=list, blank=True, verbose_name="Tender Specifications (AI observations)",
        help_text="Cached AI-generated list of notes (deadlines, guarantees, site visits, "
                  "etc.) that don't fit the description/evaluation fields.",
    )

    # Options for `specifications_ai_status`
    class AiStatusChoices(models.TextChoices):
        PENDING = 'pending', 'Nunca gerado'
        GENERATING = 'generating', 'A gerar'
        DONE = 'done', 'Concluído'
        ERROR = 'error', 'Erro'

    # Estado da geração em BACKGROUND do detalhe IA (ver anuncios/specifications_ai.py:
    # generate_detail_async). PENDING/ERROR são ambos "por gerar" — uma nova chamada a
    # /anuncios/<id>/detail/ relança a geração a partir de qualquer um dos dois.
    specifications_ai_status = models.CharField(
        max_length=10, choices=AiStatusChoices.choices, default=AiStatusChoices.PENDING,
        blank=True, verbose_name="Tender Specifications (AI status)",
        help_text="Background AI-detail generation status.",
    )

    proposal_deadline = models.DateField(null=True, blank=True, verbose_name="Proposal Deadline")

    # Options for `status`
    class StatusChoices(models.TextChoices):
        ACTIVE = 'active', 'Ativo'
        INACTIVE = 'inactive', 'Inativo'
        # Sem prazo de propostas identificado na importação — precisa de correção manual
        # antes de poder ser dado como ativo ou inativo.
        TO_FIX = 'to_fix', 'Corrigir'

    # Estado do anúncio. Calculado na importação a partir de `proposal_deadline`: sem prazo ->
    # 'to_fix'; prazo expirado -> 'inactive'; caso contrário -> 'active'. `deactivate_expired`
    # atualiza para 'inactive' os que expiram entre importações. Também editável manualmente.
    status = models.CharField(
        max_length=10, choices=StatusChoices.choices, default=StatusChoices.ACTIVE,
        db_index=True, verbose_name="Status",
    )

    # Origem da ÚLTIMA escrita neste anúncio: 'scrape' (import do base.gov.pt) ou 'manual'
    # (PUT/PATCH em /anuncios/<id>/edit/). Ver Grant.last_update_source (mesma semântica).
    SOURCE_SCRAPE = "scrape"
    SOURCE_MANUAL = "manual"
    LAST_UPDATE_SOURCE_CHOICES = [(SOURCE_SCRAPE, "Scrape/Importação"), (SOURCE_MANUAL, "Manual")]
    last_update_source = models.CharField(
        max_length=10, choices=LAST_UPDATE_SOURCE_CHOICES, default=SOURCE_SCRAPE, db_index=True,
    )
    # Utilizador que fez a última edição MANUAL; None quando a última escrita foi da importação.
    last_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )

    # Embedding (pgvector) para pesquisa semântica — gerado pela OpenAI.
    activity_embedding = VectorField(dimensions=EMBEDDING_DIM, null=True, blank=True)
    activity_embedding_hash = models.CharField(max_length=200, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.notice_number} - {self.entity_name}"

    class Meta:
        verbose_name = "Notice"
        verbose_name_plural = "Notices"
        ordering = ['-publication_date']
