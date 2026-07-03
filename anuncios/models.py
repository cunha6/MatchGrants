from django.db import models


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

    proposal_deadline = models.DateField(null=True, blank=True, verbose_name="Proposal Deadline")

    # Active = still within the proposal deadline. Updated on import; the listing also
    # filters dynamically by date so expired notices are never shown.
    active = models.BooleanField(default=True, db_index=True, verbose_name="Active")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.notice_number} - {self.entity_name}"

    class Meta:
        verbose_name = "Notice"
        verbose_name_plural = "Notices"
        ordering = ['-publication_date']
