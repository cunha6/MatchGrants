"""Database router: keeps the NIF enrichment model in the separate 'nif' SQLite DB."""


class NifRouter:
    """Route match.NifCompany to the 'nif' database; everything else to 'default'.

    Also confines migrations: NifCompany migrates only on 'nif', and the 'nif' database
    holds nothing else (no auth/contenttypes tables there).
    """

    NIF_APP = "match"
    NIF_MODEL = "nifcompany"
    NIF_DB = "nif"

    def _is_nif_model(self, model) -> bool:
        # app_label + model_name: outro modelo homónimo noutra app não é desviado.
        return (model._meta.app_label == self.NIF_APP
                and model._meta.model_name == self.NIF_MODEL)

    def db_for_read(self, model, **hints):
        if self._is_nif_model(model):
            return self.NIF_DB
        return None

    def db_for_write(self, model, **hints):
        if self._is_nif_model(model):
            return self.NIF_DB
        return None

    def allow_relation(self, obj1, obj2, **hints):
        # NifCompany has no relations to other models; leave the rest to Django.
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == self.NIF_APP and model_name == self.NIF_MODEL:
            return db == self.NIF_DB      # NifCompany only on 'nif'
        if db == self.NIF_DB:
            return False                  # nothing else lives in the 'nif' DB
        return None
