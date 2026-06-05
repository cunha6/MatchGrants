from django.db import models


class Aviso(models.Model):
    ORIGEM_CHOICES = [
        ("compete", "Compete2030"),
        ("portugal", "Portugal2030"),
        ("prr", "PRR"),
    ]

    # Metadata de scraping
    origem = models.CharField(max_length=20, choices=ORIGEM_CHOICES, db_index=True)
    url_scraping = models.URLField(max_length=500, unique=True)
    caminho_pdf = models.CharField(max_length=500, blank=True)
    caminho_markdown = models.CharField(max_length=500, blank=True)
    processado_ia = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    # P1 — identificação
    codigo_aviso = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    titulo = models.TextField(blank=True, null=True)
    programa_financiador = models.TextField(blank=True, null=True)
    entidade_gestora = models.TextField(blank=True, null=True)
    data_publicacao = models.CharField(max_length=50, blank=True, null=True)
    data_republicacao = models.CharField(max_length=50, blank=True, null=True)
    ultima_republicacao = models.CharField(max_length=50, blank=True, null=True)
    data_alteracao = models.CharField(max_length=50, blank=True, null=True)
    modalidade_aviso = models.CharField(max_length=100, blank=True, null=True)
    objetivo = models.TextField(blank=True, null=True)
    nome_fundo = models.CharField(max_length=100, blank=True, null=True)
    prioridade_programa = models.TextField(blank=True, null=True)
    tipo_intervencao_codigo = models.TextField(blank=True, null=True)
    duracao_maxima_meses = models.IntegerField(null=True, blank=True)
    caes_elegiveis = models.JSONField(default=list, blank=True)
    regioes_admissiveis = models.JSONField(default=list, blank=True)
    data_inicio_elegibilidade_despesa = models.CharField(max_length=50, blank=True, null=True)
    objetivo_especifico = models.TextField(blank=True, null=True)
    tipologia_operacao = models.TextField(blank=True, null=True)
    acoes_abrangidas = models.TextField(blank=True, null=True)
    organismos_intermedios = models.JSONField(default=list, blank=True)
    legislacao_aplicavel = models.JSONField(default=list, blank=True)
    documentos_regulamentacao = models.JSONField(default=list, blank=True)
    setores_tecnologicos_alvo = models.JSONField(default=list, blank=True)
    submissao_candidaturas = models.TextField(blank=True, null=True)
    criterios_elegibilidade_beneficiario = models.JSONField(default=list, blank=True)
    destinatarios_finais = models.JSONField(default=list, blank=True)
    principio_dnsh = models.TextField(blank=True, null=True)
    requisitos_compromisso = models.TextField(blank=True, null=True)
    # P2 — território e execução
    dotacao_global = models.FloatField(null=True, blank=True)
    territorios_baixa_densidade = models.JSONField(default=list, blank=True)
    limites_submissao = models.TextField(blank=True, null=True)
    data_limite_execucao_absoluta = models.CharField(max_length=50, blank=True, null=True)
    metas_execucao_financeira = models.JSONField(default=list, blank=True)
    # P3 — financeiro
    investimento_minimo = models.FloatField(null=True, blank=True)
    investimento_maximo = models.FloatField(null=True, blank=True)
    limite_autofinanciamento_exigido = models.FloatField(null=True, blank=True)
    regime_auxilio_estado = models.TextField(blank=True, null=True)
    artigo_rgbc_aplicavel = models.TextField(blank=True, null=True)
    contacto = models.JSONField(default=list, blank=True)
    formas_pagamento = models.JSONField(default=list, blank=True)
    # P4 — avaliação
    criterios_selecao_projeto = models.JSONField(default=list, blank=True)
    # P5 — despesas e indicadores
    despesas_elegiveis = models.JSONField(default=list, blank=True)
    despesas_nao_elegiveis = models.JSONField(default=list, blank=True)
    indicadores_realizacao = models.JSONField(default=list, blank=True)
    indicadores_resultados = models.JSONField(default=list, blank=True)
    indicadores_acompanhamento = models.JSONField(default=list, blank=True)
    obrigacoes_beneficiarios = models.JSONField(default=list, blank=True)
    obrigacoes_comunicacao = models.JSONField(default=list, blank=True)
    # P6 — documentos
    documentos_candidatura = models.JSONField(default=list, blank=True)
    # P7 — temas dos Anexos não capturados (para revisão futura)
    aprofundar = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name = "Aviso"
        verbose_name_plural = "Avisos"

    def __str__(self):
        return self.codigo_aviso or self.titulo or f"Aviso #{self.pk}"



class BeneficiarioPorAcao(models.Model):
    aviso = models.ForeignKey(Aviso, on_delete=models.CASCADE, related_name="beneficiarios_por_acao")
    tipo_acao = models.TextField(blank=True, null=True)
    entidades = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name_plural = "Beneficiários por Ação"


class Fase(models.Model):
    aviso = models.ForeignKey(Aviso, on_delete=models.CASCADE, related_name="fases")
    codigo_fase = models.CharField(max_length=100, blank=True, null=True)
    nome = models.CharField(max_length=200, blank=True, null=True)
    data_inicio = models.CharField(max_length=50, blank=True, null=True)
    data_fim = models.CharField(max_length=50, blank=True, null=True)
    condicao_acesso = models.TextField(blank=True, null=True)


class AreaAbrangida(models.Model):
    aviso = models.ForeignKey(Aviso, on_delete=models.CASCADE, related_name="areas_abrangidas")
    codigo_area = models.CharField(max_length=100, blank=True, null=True)
    area_geografica = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name_plural = "Áreas Abrangidas"


class FaseArea(models.Model):
    aviso = models.ForeignKey(Aviso, on_delete=models.CASCADE, related_name="fase_areas")
    codigo_fase = models.CharField(max_length=100, blank=True, null=True)
    codigo_area = models.CharField(max_length=100, blank=True, null=True)
    dotacao_orcamental = models.FloatField(null=True, blank=True)
    taxa_financiamento_maxima = models.FloatField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "Fases por Área"


class TaxaFinanciamento(models.Model):
    aviso = models.ForeignKey(Aviso, on_delete=models.CASCADE, related_name="taxas_financiamento")
    codigo_taxa = models.CharField(max_length=100, blank=True, null=True)
    dimensao_empresa = models.TextField(blank=True, null=True)
    regime_auxilio = models.TextField(blank=True, null=True)
    taxa_base = models.CharField(max_length=50, blank=True, null=True)
    majoracao_regional = models.CharField(max_length=50, blank=True, null=True)
    taxa_maxima_global = models.CharField(max_length=50, blank=True, null=True)
    limite_acumulacao_minimis = models.FloatField(null=True, blank=True)
    condicao_especifica = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name_plural = "Taxas de Financiamento"


class LimiteDespesa(models.Model):
    aviso = models.ForeignKey(Aviso, on_delete=models.CASCADE, related_name="limites_despesa")
    codigo_limite = models.CharField(max_length=100, blank=True, null=True)
    rubrica_despesa = models.TextField(blank=True, null=True)
    metodologia_ocs_aplicavel = models.TextField(blank=True, null=True)
    valor_maximo_absoluto = models.FloatField(null=True, blank=True)
    valor_maximo_percentual = models.FloatField(null=True, blank=True)
    base_calculo = models.TextField(blank=True, null=True)
    condicoes_especifica = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name_plural = "Limites de Despesa"


class PenalizacaoIncumprimento(models.Model):
    aviso = models.ForeignKey(Aviso, on_delete=models.CASCADE, related_name="penalizacoes")
    codigo_penalizacao = models.CharField(max_length=100, blank=True, null=True)
    tipo_indicadores = models.TextField(blank=True, null=True)
    limiar_tolerancia_geral = models.FloatField(null=True, blank=True)
    limiar_tolerancia_baixa_densidade = models.FloatField(null=True, blank=True)
    reducao_por_ponto_percentual = models.FloatField(null=True, blank=True)
    penalizacao_maxima_percentual = models.FloatField(null=True, blank=True)
    limiar_revogacao_financiamento = models.FloatField(null=True, blank=True)
    descricao_regra = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name_plural = "Penalizações por Incumprimento"


class MetodologiaAvaliacao(models.Model):
    aviso = models.ForeignKey(Aviso, on_delete=models.CASCADE, related_name="metodologias_avaliacao")
    codigo_avaliacao = models.CharField(max_length=100, blank=True, null=True)
    formula_merito_projeto = models.TextField(blank=True, null=True)
    pontuacao_minima_global = models.FloatField(null=True, blank=True)
    criterios_avaliacao = models.JSONField(default=list, blank=True)
    criterios_desempate = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name_plural = "Metodologias de Avaliação"
