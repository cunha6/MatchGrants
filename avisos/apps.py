from django.apps import AppConfig


class AvisosConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "avisos"

    def ready(self):
        from . import signals  # noqa: F401  (liga o signal que sincroniza a GrantCae)
