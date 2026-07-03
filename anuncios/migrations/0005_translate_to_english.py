"""Traduz o modelo Anuncio (PT) para Notice (EN), preservando os dados.

Renomeia o modelo e todos os campos com RenameModel/RenameField (mantém os dados;
não há remoção/recriação de colunas).
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("anuncios", "0004_alter_anuncio_data_limite_proposta"),
    ]

    operations = [
        migrations.RenameModel(old_name="Anuncio", new_name="Notice"),

        migrations.RenameField("notice", "n_anuncio", "notice_number"),
        migrations.RenameField("notice", "id_incm", "incm_id"),
        migrations.RenameField("notice", "data_publicacao", "publication_date"),
        migrations.RenameField("notice", "nif_entidade", "entity_nif"),
        migrations.RenameField("notice", "designacao_entidade", "entity_name"),
        migrations.RenameField("notice", "descricao_anuncio", "description"),
        migrations.RenameField("notice", "num_dr", "dr_number"),
        migrations.RenameField("notice", "serie", "series"),
        migrations.RenameField("notice", "tipo_acto", "act_type"),
        migrations.RenameField("notice", "tipos_contrato", "contract_types"),
        migrations.RenameField("notice", "lotes", "lots"),
        migrations.RenameField("notice", "preco_base", "base_price"),
        migrations.RenameField("notice", "modelo_anuncio", "procedure_type"),
        migrations.RenameField("notice", "ano", "year"),
        migrations.RenameField("notice", "criter_ambient", "environmental_criteria"),
        migrations.RenameField("notice", "prazo_propostas", "proposal_period_days"),
        migrations.RenameField("notice", "pecas_procedimento", "procedure_documents_url"),
        migrations.RenameField("notice", "caderno_encargos_path", "specifications_path"),
        migrations.RenameField("notice", "data_limite_proposta", "proposal_deadline"),
        migrations.RenameField("notice", "ativo", "active"),
        migrations.RenameField("notice", "criado_em", "created_at"),
        migrations.RenameField("notice", "atualizado_em", "updated_at"),

        migrations.AlterModelOptions(
            name="notice",
            options={
                "ordering": ["-publication_date"],
                "verbose_name": "Notice",
                "verbose_name_plural": "Notices",
            },
        ),
    ]
