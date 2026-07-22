"""Mantém a tabela derivada GrantCae em sincronia com Grant.included_caes/excluded_caes.

Grant.included_caes/excluded_caes (JSONField) é a FONTE DE VERDADE — escrita pela extração e
alterada pela edição. GrantCae é um índice derivado para o prefiltro SQL do match. Este signal
reconstrói as linhas sempre que os campos CAE de um aviso mudam (extração ou edição), sem que o
resto do código precise de saber da GrantCae.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from common.cae import cae_pattern_prefix
from .models import Grant, GrantCae

_CAE_FIELDS = {"included_caes", "excluded_caes"}


def sync_grant_cae(grant: Grant) -> None:
    """Reconstrói as linhas GrantCae do aviso a partir de included_caes/excluded_caes."""
    rows = []
    for kind, patterns in (
        (GrantCae.INCLUDED, grant.included_caes or []),
        (GrantCae.EXCLUDED, grant.excluded_caes or []),
    ):
        for pattern in patterns:
            prefix = cae_pattern_prefix(pattern)
            if prefix:
                rows.append(GrantCae(grant=grant, prefix=prefix, kind=kind))
    grant.cae_entries.all().delete()
    if rows:
        GrantCae.objects.bulk_create(rows)


@receiver(post_save, sender=Grant)
def _resync_cae_on_save(sender, instance, update_fields=None, **kwargs):
    # Só reconstrói quando os campos CAE podem ter mudado. Os saves com `update_fields` que não
    # tocam nos CAE (ex: o save do embedding) são ignorados — evita reconstruções inúteis.
    if update_fields is not None and not (_CAE_FIELDS & set(update_fields)):
        return
    sync_grant_cae(instance)
