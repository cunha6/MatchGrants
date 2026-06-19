from django.db import migrations, models


def to_empty_list(apps, schema_editor):
    """Set every existing secondary_cae to a valid empty JSON array so the
    varchar -> jsonb cast (USING secondary_cae::jsonb) succeeds."""
    UserProfile = apps.get_model("users", "UserProfile")
    UserProfile.objects.update(secondary_cae="[]")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0008_alter_userprofile_main_cae_alter_userprofile_nif_and_more"),
    ]

    operations = [
        migrations.RunPython(to_empty_list, noop),
        migrations.AlterField(
            model_name="userprofile",
            name="secondary_cae",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
