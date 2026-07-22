"""Popula a tabela derivada GrantCae a partir dos included_caes/excluded_caes já existentes.

Auto-contida (não importa código da app, que pode mudar): replica a extração do prefixo do
padrão CAE. A partir daqui, o signal mantém a tabela em sincronia; esta migração só cobre os
avisos que já estavam na BD antes de a tabela existir.
"""

from django.db import migrations


def _prefix(pattern):
    s = str(pattern).strip()
    star = s.find("*")
    p = s if star == -1 else s[:star]
    return p if p.isdigit() else None


def populate(apps, schema_editor):
    Grant = apps.get_model("avisos", "Grant")
    GrantCae = apps.get_model("avisos", "GrantCae")
    rows = []
    for grant in Grant.objects.all().only("id", "included_caes", "excluded_caes"):
        for kind, patterns in (("included", grant.included_caes or []),
                               ("excluded", grant.excluded_caes or [])):
            for pattern in patterns:
                prefix = _prefix(pattern)
                if prefix:
                    rows.append(GrantCae(grant_id=grant.id, prefix=prefix, kind=kind))
    GrantCae.objects.bulk_create(rows, batch_size=1000)


def clear(apps, schema_editor):
    apps.get_model("avisos", "GrantCae").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('avisos', '0027_grantcae_grant_grant_active_processed_idx_and_more'),
    ]

    operations = [
        migrations.RunPython(populate, clear),
    ]
