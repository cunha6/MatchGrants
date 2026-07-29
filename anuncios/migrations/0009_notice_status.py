from datetime import date

from django.db import migrations, models


def _forwards(apps, schema_editor):
    Notice = apps.get_model('anuncios', 'Notice')
    today = date.today()
    Notice.objects.filter(proposal_deadline__isnull=True).update(status='to_fix')
    Notice.objects.filter(proposal_deadline__lt=today).update(status='inactive')
    Notice.objects.filter(proposal_deadline__gte=today).update(status='active')


def _backwards(apps, schema_editor):
    Notice = apps.get_model('anuncios', 'Notice')
    Notice.objects.filter(status='active').update(active=True)
    Notice.objects.exclude(status='active').update(active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('anuncios', '0008_notice_last_update_source_notice_last_updated_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='notice',
            name='status',
            field=models.CharField(
                choices=[('active', 'Ativo'), ('inactive', 'Inativo'), ('to_fix', 'Corrigir')],
                db_index=True, default='active', max_length=10,
            ),
        ),
        migrations.RunPython(_forwards, _backwards),
        migrations.RemoveField(
            model_name='notice',
            name='active',
        ),
    ]
