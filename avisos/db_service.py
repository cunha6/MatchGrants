from .models import (
    Aviso, BeneficiarioPorAcao, Fase, AreaAbrangida,
    FaseArea, TaxaFinanciamento, LimiteDespesa, PenalizacaoIncumprimento,
    MetodologiaAvaliacao,
)

_AVISO_IA_FIELDS = [
    "codigo_aviso", "titulo", "programa_financiador", "entidade_gestora",
    "data_publicacao", "data_republicacao", "ultima_republicacao", "data_alteracao",
    "modalidade_aviso", "objetivo", "nome_fundo", "prioridade_programa",
    "tipo_intervencao_codigo", "duracao_maxima_meses", "caes_elegiveis",
    "regioes_admissiveis", "data_inicio_elegibilidade_despesa", "objetivo_especifico",
    "tipologia_operacao", "acoes_abrangidas", "organismos_intermedios",
    "legislacao_aplicavel", "documentos_regulamentacao", "setores_tecnologicos_alvo",
    "submissao_candidaturas", "criterios_elegibilidade_beneficiario", "destinatarios_finais",
    "principio_dnsh", "requisitos_compromisso", "dotacao_global",
    "territorios_baixa_densidade", "limites_submissao", "data_limite_execucao_absoluta",
    "metas_execucao_financeira", "investimento_minimo", "investimento_maximo",
    "limite_autofinanciamento_exigido", "regime_auxilio_estado", "artigo_rgbc_aplicavel",
    "contacto", "formas_pagamento", "criterios_selecao_projeto", "despesas_elegiveis",
    "despesas_nao_elegiveis", "indicadores_realizacao", "indicadores_resultados",
    "indicadores_acompanhamento", "obrigacoes_beneficiarios", "obrigacoes_comunicacao",
    "documentos_candidatura", "aprofundar",
]


def guardar_aviso_scraping(aviso_dict: dict, origem: str) -> Aviso | None:
    url = aviso_dict.get("url") or aviso_dict.get("url_scraping")
    if not url:
        return None

    aviso, _ = Aviso.objects.get_or_create(
        url_scraping=url,
        defaults={"origem": origem},
    )
    changed = False
    if origem and not aviso.origem:
        aviso.origem = origem
        changed = True
    if aviso_dict.get("titulo") and not aviso.titulo:
        aviso.titulo = aviso_dict["titulo"]
        changed = True
    if aviso_dict.get("codigo_aviso") and not aviso.codigo_aviso:
        aviso.codigo_aviso = aviso_dict["codigo_aviso"]
        changed = True
    if changed:
        aviso.save()
    return aviso


def guardar_aviso_ia(
    dados_ia: dict,
    url_scraping: str = "",
    caminho_pdf: str = "",
    caminho_markdown: str = "",
    force_overwrite: bool = False,
) -> Aviso | None:
    aviso_data = dados_ia.get("Aviso", {})
    codigo_aviso = aviso_data.get("codigo_aviso")

    aviso = None
    if url_scraping:
        aviso = Aviso.objects.filter(url_scraping=url_scraping).first()
    if aviso is None and codigo_aviso:
        aviso = Aviso.objects.filter(codigo_aviso=codigo_aviso).first()
    if aviso is None:
        aviso = Aviso(url_scraping=url_scraping or f"unknown:{codigo_aviso}", origem="")

    for field in _AVISO_IA_FIELDS:
        value = aviso_data.get(field)
        if value not in (None, [], ""):
            if force_overwrite or getattr(aviso, field, None) in (None, [], ""):
                setattr(aviso, field, value)

    if caminho_pdf:
        aviso.caminho_pdf = caminho_pdf
    if caminho_markdown:
        aviso.caminho_markdown = caminho_markdown
    aviso.processado_ia = True
    aviso.save()

    aviso.beneficiarios_por_acao.all().delete()
    aviso.fases.all().delete()
    aviso.areas_abrangidas.all().delete()
    aviso.fase_areas.all().delete()
    aviso.taxas_financiamento.all().delete()
    aviso.limites_despesa.all().delete()
    aviso.penalizacoes.all().delete()
    aviso.metodologias_avaliacao.all().delete()

    BeneficiarioPorAcao.objects.bulk_create([
        BeneficiarioPorAcao(
            aviso=aviso,
            tipo_acao=b.get("tipo_acao"),
            entidades=b.get("entidades", []),
        ) for b in dados_ia.get("Beneficiario_Por_Acao", [])
    ])

    Fase.objects.bulk_create([
        Fase(
            aviso=aviso,
            codigo_fase=f.get("codigo_fase"),
            nome=f.get("nome"),
            data_inicio=f.get("data_inicio"),
            data_fim=f.get("data_fim"),
            condicao_acesso=f.get("condicao_acesso"),
        ) for f in dados_ia.get("fases", [])
    ])

    AreaAbrangida.objects.bulk_create([
        AreaAbrangida(
            aviso=aviso,
            codigo_area=a.get("codigo_area"),
            area_geografica=a.get("area_geografica"),
        ) for a in dados_ia.get("Area_Abrangida", [])
    ])

    FaseArea.objects.bulk_create([
        FaseArea(
            aviso=aviso,
            codigo_fase=fa.get("codigo_fase"),
            codigo_area=fa.get("codigo_area"),
            dotacao_orcamental=fa.get("dotacao_orcamental"),
            taxa_financiamento_maxima=fa.get("taxa_financiamento_maxima"),
        ) for fa in dados_ia.get("Fase_Area", [])
    ])

    TaxaFinanciamento.objects.bulk_create([
        TaxaFinanciamento(
            aviso=aviso,
            codigo_taxa=t.get("codigo_taxa"),
            dimensao_empresa=t.get("dimensao_empresa"),
            regime_auxilio=t.get("regime_auxilio"),
            taxa_base=t.get("taxa_base"),
            majoracao_regional=t.get("majoracao_regional"),
            taxa_maxima_global=t.get("taxa_maxima_global"),
            limite_acumulacao_minimis=t.get("limite_acumulacao_minimis"),
            condicao_especifica=t.get("condicao_especifica"),
        ) for t in dados_ia.get("Taxa_Financiamento", [])
    ])

    LimiteDespesa.objects.bulk_create([
        LimiteDespesa(
            aviso=aviso,
            codigo_limite=l.get("codigo_limite"),
            rubrica_despesa=l.get("rubrica_despesa"),
            metodologia_ocs_aplicavel=l.get("metodologia_ocs_aplicavel"),
            valor_maximo_absoluto=l.get("valor_maximo_absoluto"),
            valor_maximo_percentual=l.get("valor_maximo_percentual"),
            base_calculo=l.get("base_calculo"),
            condicoes_especifica=l.get("condicoes_especifica"),
        ) for l in dados_ia.get("Limite_Despesa", [])
    ])

    PenalizacaoIncumprimento.objects.bulk_create([
        PenalizacaoIncumprimento(
            aviso=aviso,
            codigo_penalizacao=p.get("codigo_penalizacao"),
            tipo_indicadores=p.get("tipo_indicadores"),
            limiar_tolerancia_geral=p.get("limiar_tolerancia_geral"),
            limiar_tolerancia_baixa_densidade=p.get("limiar_tolerancia_baixa_densidade"),
            reducao_por_ponto_percentual=p.get("reducao_por_ponto_percentual"),
            penalizacao_maxima_percentual=p.get("penalizacao_maxima_percentual"),
            limiar_revogacao_financiamento=p.get("limiar_revogacao_financiamento"),
            descricao_regra=p.get("descricao_regra"),
        ) for p in dados_ia.get("Penalizacao_Incumprimento", [])
    ])

    MetodologiaAvaliacao.objects.bulk_create([
        MetodologiaAvaliacao(
            aviso=aviso,
            codigo_avaliacao=m.get("codigo_avaliacao"),
            formula_merito_projeto=m.get("formula_merito_projeto"),
            pontuacao_minima_global=m.get("pontuacao_minima_global"),
            criterios_avaliacao=m.get("criterios_avaliacao", []),
            criterios_desempate=m.get("criterios_desempate", []),
        ) for m in dados_ia.get("Metodologia_Avaliacao", [])
    ])

    return aviso
