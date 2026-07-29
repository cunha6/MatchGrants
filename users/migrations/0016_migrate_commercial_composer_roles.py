"""Migra dados dos roles antigos ('commercial', 'composer') para o novo modelo: o antigo
'commercial' passa a 'commercial_grants' (o composer não tinha utilizadores, mas por segurança
cai em 'client' se algum dia existir)."""

from django.db import migrations


def forwards(apps, schema_editor):
    UserProfile = apps.get_model("users", "UserProfile")
    UserProfile.objects.filter(role="commercial").update(role="commercial_grants")
    UserProfile.objects.filter(role="composer").update(role="client")


def backwards(apps, schema_editor):
    UserProfile = apps.get_model("users", "UserProfile")
    UserProfile.objects.filter(role="commercial_grants").update(role="commercial")
    UserProfile.objects.filter(role="commercial_public").update(role="commercial")


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0015_alter_userprofile_role"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
