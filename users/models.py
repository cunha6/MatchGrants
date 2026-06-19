from django.contrib.auth.models import User
from django.core.validators import MinLengthValidator
from django.db import models


class UserProfile(models.Model):
    # --- Roles ---
    ADMIN = "admin"
    COMMERCIAL = "commercial"
    COMPOSER = "composer"
    CLIENT = "client"
    ROLE_CHOICES = [
        (ADMIN, "Admin"),
        (COMMERCIAL, "Commercial"),
        (COMPOSER, "Composer"),
        (CLIENT, "Client"),
    ]

    EMPRESA = "empresa"
    ASSOCIACAO = "associacao"
    MUNICIPIO = "municipio"
    ENSINO = "ensino"
    ONG = "ong"
    OUTRO = "outro"
    EntityType = [
        (EMPRESA, EMPRESA),
        (ASSOCIACAO, ASSOCIACAO),
        (MUNICIPIO, MUNICIPIO),
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
    entity_type = models.CharField(max_length=10, choices=EntityType, blank=True, null=True)
    entity_size = models.CharField(max_length=10, choices=EntitySize, blank=True, null=True)
    incorporation_date = models.DateField(blank=True, null=True)        # incorporation date
    nif = models.CharField(max_length=9, validators=[MinLengthValidator(9)], blank=True, null=True, db_index=True)
    main_cae = models.CharField(max_length=5, validators=[MinLengthValidator(5)], blank=True, null=True)   # main CAE
    secondary_cae = models.JSONField(default=list, blank=True)          # list of secondary CAE codes
    address = models.CharField(max_length=255, blank=True, null=True)                   # address
    region = models.CharField(max_length=100, blank=True, null=True)    # region (free text)
    nuts_ii = models.BooleanField(default=False)
    nuts_iii = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.username} ({self.role})"
