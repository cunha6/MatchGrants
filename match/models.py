from django.db import models


class NifCompany(models.Model):
    """Read-only enrichment record for a company, keyed by NIF.

    Loaded from dictionary_by_nif.json into a separate SQLite database ('nif' — see
    settings.DATABASES and match/routers.py). Provides the fields nif.pt does not:
    number of employees and operating revenue, plus the company size already
    classified (micro / pequena / média / grande) at load time, and region/district
    fallbacks. `dimension` is precomputed by load_nif_dictionary via classify_dimension
    so the match reads it directly instead of recomputing per query.
    """

    MICRO = "micro"
    PEQUENA = "pequena"
    MEDIA = "media"
    GRANDE = "grande"
    DIMENSION_CHOICES = [
        (MICRO, MICRO),
        (PEQUENA, PEQUENA),
        (MEDIA, MEDIA),
        (GRANDE, GRANDE),
    ]

    nif = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=255, blank=True, default="")
    locality = models.CharField(max_length=255, blank=True, default="")
    municipality = models.CharField(max_length=255, blank=True, default="")  # Concelho
    district = models.CharField(max_length=255, blank=True, default="")
    region = models.CharField(max_length=255, blank=True, default="")
    postal_code = models.CharField(max_length=20, blank=True, default="")
    employees = models.IntegerField(null=True, blank=True)
    operating_revenue = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True,
        help_text="Proveitos Operacionais EUR (annual turnover) — last available year.",
    )
    # Dimensão UE (micro/pequena/media/grande) pré-calculada no load a partir de
    # employees + operating_revenue (ver classify_dimension). Null quando indeterminável.
    dimension = models.CharField(
        max_length=10, choices=DIMENSION_CHOICES, blank=True, null=True, db_index=True,
    )
    last_year = models.CharField(max_length=20, blank=True, default="")

    class Meta:
        verbose_name = "NIF enrichment"
        verbose_name_plural = "NIF enrichment"

    def __str__(self):
        return f"{self.nif} - {self.name}"

    def to_profile_fields(self) -> dict:
        """Dados deste registo já alinhados com os campos do `users.UserProfile`, para
        criar/atualizar um perfil a partir do SQLite a qualquer momento — ponto único de
        concordância entre os dois modelos. Inclui apenas campos com correspondência
        direta no UserProfile (`district` e `name` não têm equivalente e ficam de fora).

            NifCompany           UserProfile
            ----------------     -----------
            region           ->  region
            locality         ->  city
            municipality     ->  county        (concelho)
            postal_code      ->  postal_code
            dimension        ->  entity_size   (mesmas choices: micro/pequena/media/grande)
            nif              ->  nif
        """
        return {
            "nif": self.nif,
            "region": self.region,
            "city": self.locality,
            "county": self.municipality,
            "postal_code": self.postal_code,
            "entity_size": self.dimension,
        }
