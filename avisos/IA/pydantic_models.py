"""Pydantic models — validam e normalizam o output do LLM antes do merge."""

from typing import Annotated, Any, Optional
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


# ---------------------------------------------------------------------------
# Coercers reutilizáveis
def _float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("€", "").replace("%", "").replace(" ", "")
    if s.lower() in ("null", "none", "n/a", "-", ""):
        return None
    if "," in s and s.count(",") == 1 and s.index(",") > s.rfind("."):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if s.lower() in ("null", "none", "n/a", "-", ""):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in ("null", "none", "n/a", "") else s


def _list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [v] if v.strip() and v.strip().lower() not in ("null", "none") else []
    if isinstance(v, list):
        result = []
        for item in v:
            if item is None:
                continue
            s = str(item).strip()
            if s and s.lower() not in ("null", "none"):
                result.append(s)
        return result
    return []


def _bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).lower().strip()
    if s in ("true", "yes", "1", "sim", "verdadeiro"):
        return True
    if s in ("false", "no", "0", "não", "falso", "nao"):
        return False
    return None


CoercedFloat = Annotated[Optional[float],  BeforeValidator(_float)]
CoercedInt   = Annotated[Optional[int],    BeforeValidator(_int)]
CoercedStr   = Annotated[Optional[str],    BeforeValidator(_str)]
CoercedList  = Annotated[list[str],        BeforeValidator(_list)]
CoercedBool  = Annotated[Optional[bool],   BeforeValidator(_bool)]


# Modelo base
class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# Sub-modelos reutilizáveis
class Indicador(_Base):
    codigo_indicador: CoercedStr   = None
    descricao:        CoercedStr   = None
    unidade_medida:   CoercedStr   = None
    meta:             CoercedStr   = None
    metodo_calculo:   CoercedStr   = None


class DocumentoRegulamentacao(_Base):
    nome: CoercedStr = None
    url:  CoercedStr = None


class DocumentoCandidatura(_Base):
    nome:                              CoercedStr  = None
    obrigatorio:                       CoercedBool = None
    tipo_documento:                    CoercedStr  = None
    prova_maturidade:                  CoercedBool = None
    restricoes_formato_anexo_tecnico:  CoercedStr  = None


class OrganismoIntermedio(_Base):
    nome:        CoercedStr = None
    nif:         CoercedStr = None
    competencias: CoercedStr = None



# Entidades finais
class Aviso(_Base):
    # P1 — identificação
    codigo_aviso:                         CoercedStr   = None
    titulo:                               CoercedStr   = None
    programa_financiador:                 CoercedStr   = None
    entidade_gestora:                     CoercedStr   = None
    data_publicacao:                      CoercedStr   = None
    data_republicacao:                    CoercedStr   = None
    ultima_republicacao:                  CoercedStr   = None
    data_alteracao:                       CoercedStr   = None
    modalidade_aviso:                     CoercedStr   = None
    objetivo:                             CoercedStr   = None
    nome_fundo:                           CoercedStr   = None
    prioridade_programa:                  CoercedStr   = None
    tipo_intervencao_codigo:              CoercedStr   = None
    duracao_maxima_meses:                 CoercedInt   = None
    caes_elegiveis:                       CoercedList  = Field(default_factory=list)
    regioes_admissiveis:                  CoercedList  = Field(default_factory=list)
    data_inicio_elegibilidade_despesa:    CoercedStr   = None
    objetivo_especifico:                  CoercedStr   = None
    tipologia_operacao:                   CoercedStr   = None
    acoes_abrangidas:                     CoercedStr   = None
    organismos_intermedios:               list[OrganismoIntermedio] = Field(default_factory=list)
    legislacao_aplicavel:                 CoercedList  = Field(default_factory=list)
    documentos_regulamentacao:            list[DocumentoRegulamentacao] = Field(default_factory=list)
    setores_tecnologicos_alvo:            CoercedList  = Field(default_factory=list)
    submissao_candidaturas:               CoercedStr   = None
    criterios_elegibilidade_beneficiario: CoercedList  = Field(default_factory=list)
    destinatarios_finais:                 CoercedList  = Field(default_factory=list)
    principio_dnsh:                       CoercedStr   = None
    requisitos_compromisso:               CoercedStr   = None
    # P2 — território e execução
    dotacao_global:                       CoercedFloat = None
    territorios_baixa_densidade:          CoercedList  = Field(default_factory=list)
    limites_submissao:                    CoercedStr   = None
    data_limite_execucao_absoluta:        CoercedStr   = None
    metas_execucao_financeira:            CoercedList  = Field(default_factory=list)
    # P3 — financeiro
    investimento_minimo:                  CoercedFloat = None
    investimento_maximo:                  CoercedFloat = None
    limite_autofinanciamento_exigido:     CoercedFloat = None
    regime_auxilio_estado:                CoercedStr   = None
    artigo_rgbc_aplicavel:                CoercedStr   = None
    contacto:                             CoercedList  = Field(default_factory=list)
    formas_pagamento:                     CoercedList  = Field(default_factory=list)
    # P4 — avaliação
    criterios_selecao_projeto:            CoercedList  = Field(default_factory=list)
    # P5 — despesas e indicadores
    despesas_elegiveis:                   CoercedList  = Field(default_factory=list)
    despesas_nao_elegiveis:               CoercedList  = Field(default_factory=list)
    indicadores_realizacao:               list[Indicador] = Field(default_factory=list)
    indicadores_resultados:               list[Indicador] = Field(default_factory=list)
    indicadores_acompanhamento:           list[Indicador] = Field(default_factory=list)
    obrigacoes_beneficiarios:             CoercedList  = Field(default_factory=list)
    obrigacoes_comunicacao:               CoercedList  = Field(default_factory=list)
    # P6 — documentos
    documentos_candidatura:               list[DocumentoCandidatura] = Field(default_factory=list)
    # P7 — temas dos Anexos não capturados
    aprofundar:                           CoercedList  = Field(default_factory=list)


class BeneficiarioPorAcao(_Base):
    codigo_aviso: CoercedStr  = None
    tipo_acao:    CoercedStr  = None
    entidades:    CoercedList = Field(default_factory=list)


class TaxaFinanciamento(_Base):
    codigo_taxa:               CoercedStr   = None
    codigo_aviso:              CoercedStr   = None
    dimensao_empresa:          CoercedStr   = None
    regime_auxilio:            CoercedStr   = None
    taxa_base:                 CoercedStr   = None
    majoracao_regional:        CoercedStr   = None
    taxa_maxima_global:        CoercedStr   = None
    limite_acumulacao_minimis: CoercedFloat = None
    condicao_especifica:       CoercedStr   = None


class LimiteDespesa(_Base):
    codigo_limite:            CoercedStr   = None
    codigo_aviso:             CoercedStr   = None
    rubrica_despesa:          CoercedStr   = None
    metodologia_ocs_aplicavel: CoercedStr  = None
    valor_maximo_absoluto:    CoercedFloat = None
    valor_maximo_percentual:  CoercedFloat = None
    base_calculo:             CoercedStr   = None
    condicoes_especifica:     CoercedStr   = None


class PenalizacaoIncumprimento(_Base):
    codigo_penalizacao:                CoercedStr   = None
    codigo_aviso:                      CoercedStr   = None
    tipo_indicadores:                  CoercedStr   = None
    formula_grau_cumprimento:          CoercedStr   = None
    limiar_tolerancia_geral:           CoercedFloat = None
    limiar_tolerancia_baixa_densidade: CoercedFloat = None
    reducao_por_ponto_percentual:      CoercedFloat = None
    penalizacao_maxima_percentual:     CoercedFloat = None
    limiar_revogacao_financiamento:    CoercedFloat = None
    descricao_regra:                   CoercedStr   = None


class CriterioAvaliacao(_Base):
    codigo_criterio:                   CoercedStr   = None
    descricao:                         CoercedStr   = None
    ponderacao:                        CoercedFloat = None
    pontuacao_minima:                  CoercedFloat = None
    pontuacao_minima_criterio_exclusao: Optional[bool] = None


class MetodologiaAvaliacao(_Base):
    codigo_avaliacao:        CoercedStr   = None
    codigo_aviso:            CoercedStr   = None
    formula_merito_projeto:  CoercedStr   = None
    pontuacao_minima_global: CoercedFloat = None
    criterios_avaliacao:     list[CriterioAvaliacao] = Field(default_factory=list)
    criterios_desempate:     CoercedList  = Field(default_factory=list)


class Fase(_Base):
    codigo_fase:     CoercedStr = None
    codigo_aviso:    CoercedStr = None
    nome:            CoercedStr = None
    data_inicio:     CoercedStr = None
    data_fim:        CoercedStr = None
    condicao_acesso: CoercedStr = None


class AreaAbrangida(_Base):
    codigo_area:     CoercedStr = None
    codigo_aviso:    CoercedStr = None
    area_geografica: CoercedStr = None


class FaseArea(_Base):
    codigo_fase:              CoercedStr   = None
    codigo_area:              CoercedStr   = None
    codigo_aviso:             CoercedStr   = None
    dotacao_orcamental:       CoercedFloat = None
    taxa_financiamento_maxima: CoercedFloat = None


# Parse functions — uma por prompt (P1–P6)

def parse_p1(raw: dict) -> tuple[dict, list[BeneficiarioPorAcao]]:
    from pydantic import TypeAdapter
    p1   = raw.get("Aviso_Parte1") or {}
    p1.pop("fundo_financiador", None)  # descartado — dotacao_global vem do P2
    bpas = TypeAdapter(list[BeneficiarioPorAcao]).validate_python(raw.get("Beneficiario_Por_Acao") or [])
    return p1, bpas


def parse_p2(raw: dict) -> tuple[dict, list[Fase], list[AreaAbrangida], list[FaseArea]]:
    from pydantic import TypeAdapter
    aviso = raw.get("Aviso") or {}
    fases = TypeAdapter(list[Fase]).validate_python(raw.get("fases") or [])
    areas = TypeAdapter(list[AreaAbrangida]).validate_python(raw.get("Area_Abrangida") or [])
    fa    = TypeAdapter(list[FaseArea]).validate_python(raw.get("Fase_Area") or [])
    return aviso, fases, areas, fa


def parse_p3(raw: dict) -> tuple[dict, list[TaxaFinanciamento]]:
    from pydantic import TypeAdapter
    aviso = raw.get("Aviso") or {}
    taxas = TypeAdapter(list[TaxaFinanciamento]).validate_python(raw.get("Taxa_Financiamento") or [])
    return aviso, taxas


def parse_p4(raw: dict) -> tuple[dict, list[MetodologiaAvaliacao]]:
    from pydantic import TypeAdapter
    aviso = raw.get("Aviso") or {}
    metod = TypeAdapter(list[MetodologiaAvaliacao]).validate_python(raw.get("Metodologia_Avaliacao") or [])
    return aviso, metod


def parse_p5(raw: dict) -> tuple[dict, list[LimiteDespesa], list[PenalizacaoIncumprimento]]:
    from pydantic import TypeAdapter
    aviso   = raw.get("Aviso") or {}
    limites = TypeAdapter(list[LimiteDespesa]).validate_python(raw.get("Limite_Despesa") or [])
    penal   = TypeAdapter(list[PenalizacaoIncumprimento]).validate_python(raw.get("Penalizacao_Incumprimento") or [])
    return aviso, limites, penal


def parse_p6(raw: dict) -> dict:
    from pydantic import TypeAdapter
    docs = (raw.get("Aviso") or {}).get("documentos_candidatura") or []
    return {"documentos_candidatura": TypeAdapter(list[DocumentoCandidatura]).validate_python(docs)}
