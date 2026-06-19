from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('avisos', '0008_rename_metadata_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='grant',
            name='bonus_mechanisms',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='grant',
            name='dnsh_criteria',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='grant',
            name='needs_review',
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name='GrantDocument',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('doc_type', models.CharField(choices=[('base', 'Base'), ('republication', 'Republicação'), ('amendment', 'Alteração'), ('prorrogation', 'Prorrogação'), ('rectification', 'Retificação'), ('annex', 'Anexo'), ('other', 'Outro')], db_index=True, default='other', max_length=20)),
                ('name', models.TextField(blank=True, null=True)),
                ('url', models.URLField(blank=True, max_length=500)),
                ('local_path', models.CharField(blank=True, max_length=500)),
                ('ordinal', models.IntegerField(default=0)),
                ('is_canonical', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('grant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='documents', to='avisos.grant')),
            ],
        ),
    ]
