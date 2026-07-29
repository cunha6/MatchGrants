from django.db import models


class PlannedGrant(models.Model):
    """Aviso PREVISTO no Plano Anual de Avisos do Portugal 2030.

    Não substitui `avisos.Grant`: são entidades distintas. `PlannedGrant` é a PREVISÃO do plano
    anual (o que se espera vir a abrir e quando); `Grant` é o aviso oficialmente PUBLICADO. Esta
    tabela é sincronizada a partir do Excel do plano (ver `services.sync_planned_grants`), tendo o
    `plan_id` (ID do Excel) como chave estável para o upsert.
    """

    # ID único vindo do Excel — identifica a linha do plano de forma estável entre sincronizações.
    plan_id = models.IntegerField(unique=True)
    beneficiary_type = models.TextField(blank=True, default="")     # Tipo Ent. Beneficiária
    grant_type = models.TextField(blank=True, default="")           # Natureza Aviso
    designation = models.TextField(blank=True, default="")          # Designação do Aviso
    programme = models.TextField(blank=True, default="")            # Programa
    specific_objective = models.TextField(blank=True, default="")   # Objetivo Específico
    fund = models.TextField(blank=True, default="")                 # Fundo
    total_allocation = models.DecimalField(                         # Dotação Fundo
        max_digits=18, decimal_places=2, null=True, blank=True,
    )
    expected_start = models.DateField(null=True, blank=True)        # Data Início Prevista
    expected_end = models.DateField(null=True, blank=True)          # Data Fim Prevista
    quadrimester = models.CharField(max_length=50, blank=True, default="")  # Quadrimestre
    nuts = models.TextField(blank=True, default="")                 # NUTS II
    submission_mode = models.TextField(blank=True, default="")      # Modalidade Apresentação Candidatura
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["expected_start", "plan_id"]

    def __str__(self):
        return f"{self.plan_id} · {self.designation[:60]}"
