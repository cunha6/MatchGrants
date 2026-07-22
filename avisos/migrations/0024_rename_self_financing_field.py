from django.db import migrations


class Migration(migrations.Migration):
    """Renomeia required_self_financing_limit → maximum_self_financing (semântica muda de
    'limite mínimo exigido' para 'valor máximo de autofinanciamento'). RenameField preserva
    os dados já gravados."""

    dependencies = [
        ("avisos", "0023_grant_active"),
    ]

    operations = [
        migrations.RenameField(
            model_name="grant",
            old_name="required_self_financing_limit",
            new_name="maximum_self_financing",
        ),
    ]
