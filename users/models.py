from django.contrib.auth.models import User
from django.core.validators import MinLengthValidator
from django.db import models


class UserProfile(models.Model):
    # --- Roles ---
    ADMIN = "admin"
    # Split do antigo "commercial": um por domínio de dados (avisos/grants vs. anúncios de
    # contratação pública). COMMERCIAL_PUBLIC acumula os dois (avisos + anúncios).
    COMMERCIAL_GRANTS = "commercial_grants"
    COMMERCIAL_PUBLIC = "commercial_public"
    CLIENT = "client"
    VIEWER = "viewer"
    ROLE_CHOICES = [
        (ADMIN, "Admin"),
        (COMMERCIAL_GRANTS, "Commercial Grants"),
        (COMMERCIAL_PUBLIC, "Commercial Public"),
        (CLIENT, "Client"),
        (VIEWER, "Viewer"),
    ]

    EMPRESA = "empresa"
    ASSOCIACAO = "associacao"
    COOPERATIVA = "cooperativa"
    FUNDACAO = "fundacao"
    MUNICIPIO = "municipio"
    INTERMUNICIPIO = "intermunicipio"
    MULTIMUNICIPIO = "multimunicipio"
    JUNTA_FREGUESIA = "junta_freguesia"
    MISERICORDIA = "misericordia"
    ENSINO = "ensino"
    ONG = "ong"
    OUTRO = "outro"
    EntityType = [
        (EMPRESA, EMPRESA),
        (ASSOCIACAO, ASSOCIACAO),
        (COOPERATIVA, COOPERATIVA),
        (FUNDACAO, FUNDACAO),
        (MUNICIPIO, MUNICIPIO),
        (INTERMUNICIPIO, INTERMUNICIPIO),
        (MULTIMUNICIPIO, MULTIMUNICIPIO),
        (JUNTA_FREGUESIA, JUNTA_FREGUESIA),
        (MISERICORDIA, MISERICORDIA),
        (ENSINO, ENSINO),
        (ONG, ONG),
        (OUTRO, OUTRO),
    ]

    MICRO = "micro"
    PEQUENA = "pequena"
    MEDIA = "media"
    GRANDE = "grande"
    EntitySize = [
        (MICRO, MICRO),
        (PEQUENA, PEQUENA),
        (MEDIA, MEDIA),
        (GRANDE, GRANDE),
    ]


    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=CLIENT, db_index=True)

    # --- Entity data ---
    entity_type = models.CharField(max_length=20, choices=EntityType, blank=True, null=True)
    entity_size = models.CharField(max_length=10, choices=EntitySize, blank=True, null=True)
    incorporation_date = models.DateField(blank=True, null=True)        # incorporation date
    nif = models.CharField(max_length=9, validators=[MinLengthValidator(9)], blank=True, null=True, db_index=True)
    main_cae = models.CharField(max_length=5, validators=[MinLengthValidator(5)], blank=True, null=True)   # main CAE
    secondary_cae = models.JSONField(default=list, blank=True)          # list of secondary CAE codes
    address = models.CharField(max_length=255, blank=True, null=True)                   # address
    region = models.CharField(max_length=100, blank=True, null=True)    # region (free text)
    nuts_ii = models.BooleanField(default=False)
    nuts_iii = models.BooleanField(default=False)

    # --- Dados do nif.pt (preenchidos no matching via NIF) ---
    nature = models.CharField(max_length=10, blank=True, null=True)       # código natureza jurídica (COO, SA…)
    activity = models.TextField(blank=True, null=True)                    # descrição da atividade (nif.pt), p/ match semântico
    capital = models.DecimalField(max_digits=18, decimal_places=2, blank=True, null=True)  # capital social
    capital_currency = models.CharField(max_length=10, blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    website = models.CharField(max_length=255, blank=True, null=True)
    fax = models.CharField(max_length=50, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    county = models.CharField(max_length=100, blank=True, null=True)      # concelho
    parish = models.CharField(max_length=100, blank=True, null=True)      # freguesia
    postal_code = models.CharField(max_length=20, blank=True, null=True)

    # --- Contacto (preenchido pelo pop-up do match sem login, não vem do nif.pt) ---
    job_title = models.CharField(max_length=150, blank=True, null=True)   # função na empresa

    # --- Avisos resultantes de matches (viewer OU client, ver match/views.py:evaluate_nif) ---
    # 'avisos.Grant' em string: referência preguiçosa, evita import direto (avisos->users
    # já existe; users->avisos não existia antes disto).
    # ACUMULA entre matches (a view usa .add(), não .set()): um cliente que faz vários
    # pedidos ao longo do tempo mantém o histórico de tudo o que já lhe apareceu, em vez de
    # o último match apagar os anteriores.
    matched_grants = models.ManyToManyField(
        "avisos.Grant", blank=True, related_name="matched_by_profiles",
    )

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class ClientObjective(models.Model):
    """Um objetivo/projeto que o cliente descreveu ao pedir um match (`match.evaluate_nif`).

    Texto livre — "que tipo de projeto tem em mente" — usado para reforçar a relevância
    SETORIAL do match (ver `match.services._company_sector_text`). Histórico completo: um
    cliente pode voltar a pedir matches com objetivos diferentes ao longo do tempo (um
    projeto de automação hoje, um de expansão internacional daqui a uns meses), e cada um
    fica guardado — não se sobrepõe ao anterior, ao contrário de `entity_size`/`activity`
    (que são o estado ATUAL da empresa, não um histórico de pedidos).
    """

    profile = models.ForeignKey(
        UserProfile, on_delete=models.CASCADE, related_name="objectives",
    )
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Objetivo do cliente"
        verbose_name_plural = "Objetivos do cliente"

    def __str__(self):
        return f"{self.profile.user.username}: {self.text[:60]}"
