"""Pydantic models — validam e normalizam o output do LLM antes do merge."""

import re
from typing import Annotated, Any, Optional
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

# "300.000", "3.000.000", "25.000.000": ponto como separador de MILHARES (formato PT/EU).
_THOUSANDS_DOTS = re.compile(r"-?\d{1,3}(\.\d{3})+")

# Montantes por extenso (PT): "25 milhões" → 25e6, "300 mil" → 300e3, "2 mil milhões" → 2e9.
# Ordem de teste importa: "mil milhões" antes de "milhão"; "milhar(es)" antes de "milhão"
# (ambos contêm "milh"); "mil" isolado por último. O lookbehind (?<![a-zA-Z]) (em vez de \b)
# permite o número colado à palavra ("25milhões") — o \b falha entre dígito e letra.
_MIL_MILHOES_RE = re.compile(r"(?<![a-zA-Z])mil\s+milh\w*", re.IGNORECASE)   # mil milhões = 10^9
_MILHAR_RE      = re.compile(r"(?<![a-zA-Z])milhar\w*", re.IGNORECASE)       # milhar/milhares = 10^3
_MILHAO_RE      = re.compile(r"(?<![a-zA-Z])milh\w*", re.IGNORECASE)         # milhão/milhões = 10^6
_MIL_RE         = re.compile(r"(?<![a-zA-Z])mil(?![a-zA-Z])", re.IGNORECASE)  # mil = 10^3


# ---------------------------------------------------------------------------
# Coercers reutilizáveis
def _float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    for junk in ("€", "euros", "eur", "%"):
        s = s.replace(junk, "")
    s = s.strip()
    if s in ("null", "none", "n/a", "-", ""):
        return None

    # Montantes por extenso ("25 milhões", "1,5 milhão", "300 mil", "2 mil milhões",
    # "inferior a 25 milhões euros" → o texto à volta já foi removido pelo prompt/chamador,
    # mas mesmo que sobre "25 milhões", convertemos para o número).
    mult = 1.0
    if _MIL_MILHOES_RE.search(s):
        mult = 1_000_000_000.0
        s = _MIL_MILHOES_RE.sub(" ", s)
    elif _MILHAR_RE.search(s):
        mult = 1_000.0
        s = _MILHAR_RE.sub(" ", s)
    elif _MILHAO_RE.search(s):
        mult = 1_000_000.0
        s = _MILHAO_RE.sub(" ", s)
    elif _MIL_RE.search(s):
        mult = 1_000.0
        s = _MIL_RE.sub(" ", s)

    s = s.replace(" ", "")
    if not s:
        return None
    if s.count(",") == 1 and s.rfind(",") > s.rfind("."):
        # Vírgula ÚNICA e depois do último ponto = decimal PT ("1.000,50" → 1000.50).
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # Múltiplas vírgulas (ou vírgula antes de ponto) = vírgulas são separador de milhares
        # (formato US "1,234,567") → remove-as; os pontos ficam para a regra dos milhares.
        s = s.replace(",", "")
        if _THOUSANDS_DOTS.fullmatch(s):
            s = s.replace(".", "")
    elif _THOUSANDS_DOTS.fullmatch(s):
        # Sem vírgula e com pontos a separar grupos de 3 dígitos ("300.000" = 300000,
        # NÃO 300.0). Sem estes pontos, "." é decimal (ex: "85.0", "4705882.34").
        s = s.replace(".", "")
    try:
        return float(s) * mult
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
    indicator_code: CoercedStr   = Field(default=None, alias="codigo_indicador")
    description:    CoercedStr   = Field(default=None, alias="descricao")
    unit_of_measure: CoercedStr  = Field(default=None, alias="unidade_medida")
    target:         CoercedStr   = Field(default=None, alias="meta")
    calculation_method: CoercedStr = Field(default=None, alias="metodo_calculo")


class DocumentoRegulamentacao(_Base):
    name: CoercedStr = Field(default=None, alias="nome")
    url:  CoercedStr = None


def _article_refs(v: Any) -> list[dict]:
    """Normaliza `artigos` para lista de objetos {artigo, refere_se_a}.

    Aceita também o formato legado (lista de strings) — cada string vira {"artigo": s}
    sem descrição — para não partir dados/outputs antigos.
    """
    if not v:
        return []
    if isinstance(v, (str, dict)):
        v = [v]
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for item in v:
        if item is None:
            continue
        if isinstance(item, dict):
            out.append(item)
        else:
            s = str(item).strip()
            if s and s.lower() not in ("null", "none"):
                out.append({"artigo": s})
    return out


class ArticleRef(_Base):
    # Um artigo/número específico de um diploma + a que se refere no aviso.
    article:   CoercedStr = Field(default=None, alias="artigo")
    refers_to: CoercedStr = Field(default=None, alias="refere_se_a")


CoercedArticleList = Annotated[list[ArticleRef], BeforeValidator(_article_refs)]


class LegislationRef(_Base):
    # Diploma legal estruturado: nome do regulamento + artigos (cada um com a que se refere).
    regulation_name: CoercedStr        = Field(default=None, alias="nome_regulamento")
    articles:        CoercedArticleList = Field(default_factory=list, alias="artigos")
    # Descrição geral do diploma — usar só quando o aviso não atribui os artigos a assuntos
    # distintos (ou não cita artigos). Quando há artigos, a descrição vai em cada artigo.
    refers_to:       CoercedStr        = Field(default=None, alias="refere_se_a")


def _legislation_refs(v: Any) -> list[dict]:
    """Normaliza `legislacao_aplicavel` para lista de objetos. Tolera o formato legado (lista
    de strings) — cada string vira {"nome_regulamento": s} — para não rebentar o parse quando
    o LLM devolve um diploma como texto simples em vez de objeto."""
    if not v:
        return []
    if isinstance(v, (str, dict)):
        v = [v]
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for item in v:
        if item is None:
            continue
        if isinstance(item, dict):
            out.append(item)
        else:
            s = str(item).strip()
            if s and s.lower() not in ("null", "none"):
                out.append({"nome_regulamento": s})
    return out


CoercedLegislation = Annotated[list[LegislationRef], BeforeValidator(_legislation_refs)]


class ApplicationDocument(_Base):
    name:                              CoercedStr  = Field(default=None, alias="nome")
    mandatory:                         CoercedBool = Field(default=None, alias="obrigatorio")
    document_type:                     CoercedStr  = Field(default=None, alias="tipo_documento")
    maturity_proof:                    CoercedBool = Field(default=None, alias="prova_maturidade")
    technical_annex_format_restrictions: CoercedStr = Field(default=None, alias="restricoes_formato_anexo_tecnico")


class OrganismoIntermedio(_Base):
    name:         CoercedStr = Field(default=None, alias="nome")
    tax_id:       CoercedStr = Field(default=None, alias="nif")
    competencies: CoercedStr = Field(default=None, alias="competencias")


class CriterioAvaliacao(_Base):
    # Rótulo do critério (A, A1, B1…) — é significativo (vem do documento), por isso é um
    # "name", não um código inventado. Aceita `criterion_name` (nome do campo) e o alias
    # antigo `codigo_criterio`.
    # Estrutura EM ÁRVORE: cada critério pode ter `formula` (como se combina a partir dos
    # seus filhos diretos, ex: "A = 0,6 A1 + 0,4 A2") e `subcriteria` (os filhos, aninhados).
    criterion_name:              CoercedStr   = Field(default=None, alias="codigo_criterio")
    description:                 CoercedStr   = Field(default=None, alias="descricao")
    formula:                     CoercedStr   = Field(default=None, alias="formula")
    weight:                      CoercedFloat = Field(default=None, alias="ponderacao")
    min_score:                   CoercedFloat = Field(default=None, alias="pontuacao_minima")
    is_exclusion_criterion:      Optional[bool] = Field(default=None, alias="pontuacao_minima_criterio_exclusao")
    # Folha (sem filhos) → null (não []). Pai com filhos → lista aninhada.
    subcriteria:                 Optional[list["CriterioAvaliacao"]] = Field(default=None, alias="subcriterios")

    @field_validator("subcriteria", mode="before")
    @classmethod
    def _subcriteria_empty_to_none(cls, v):
        return v or None


def _expense_groups(v: Any) -> list[dict]:
    """Normaliza despesas para lista de grupos {categoria, itens}.

    Aceita o formato legado (lista de strings) → um único grupo sem categoria
    ({"categoria": None, "itens": [...]}). Se já vier como lista de grupos (dicts),
    passa-os à frente; strings soltas à mistura vão para um grupo sem categoria.
    """
    if not v:
        return []
    if isinstance(v, (str, dict)):
        v = [v]
    if not isinstance(v, list):
        return []
    dicts = [x for x in v if isinstance(x, dict)]
    loose = [str(x).strip() for x in v
             if not isinstance(x, dict) and x is not None and str(x).strip().lower() not in ("null", "none", "")]
    groups = list(dicts)
    if loose:
        groups.append({"categoria": None, "itens": loose})
    return groups


class ExpenseGroup(_Base):
    # Grupo de despesas, OPCIONALMENTE por categoria (ex: "Indústria", "Turismo") — genérico:
    # a categoria é o rótulo que o aviso usar. `category` = null quando o aviso NÃO divide por
    # categoria (caso mais comum: um único grupo com todas as despesas).
    category: CoercedStr  = Field(default=None, alias="categoria")
    items:    CoercedList = Field(default_factory=list, alias="itens")


CoercedExpenseGroups = Annotated[list[ExpenseGroup], BeforeValidator(_expense_groups)]


# Entidades finais
class Grant(_Base):
    # P1 — identificação
    grant_code:                          CoercedStr   = Field(default=None, alias="codigo_aviso")
    title:                               CoercedStr   = Field(default=None, alias="titulo")
    financing_program:                   CoercedStr   = Field(default=None, alias="programa_financiador")
    managing_entity:                     CoercedStr   = Field(default=None, alias="entidade_gestora")
    publication_date:                    CoercedStr   = Field(default=None, alias="data_publicacao")
    republication_date:                  CoercedStr   = Field(default=None, alias="data_republicacao")
    last_republication:                  CoercedStr   = Field(default=None, alias="ultima_republicacao")
    amendment_date:                      CoercedStr   = Field(default=None, alias="data_alteracao")
    notice_modality:                     CoercedStr   = Field(default=None, alias="modalidade_aviso")
    objective:                           CoercedStr   = Field(default=None, alias="objetivo")
    fund_name:                           CoercedStr   = Field(default=None, alias="nome_fundo")
    program_priority:                    CoercedStr   = Field(default=None, alias="prioridade_programa")
    intervention_type_code:              CoercedStr   = Field(default=None, alias="tipo_intervencao_codigo")
    max_duration_months:                 CoercedInt   = Field(default=None, alias="duracao_maxima_meses")
    included_caes:                       CoercedList  = Field(default_factory=list, alias="caes_incluidos")
    excluded_caes:                       CoercedList  = Field(default_factory=list, alias="caes_excluidos")
    eligible_regions:                    CoercedList  = Field(default_factory=list, alias="regioes_admissiveis")
    expense_eligibility_start_date:      CoercedStr   = Field(default=None, alias="data_inicio_elegibilidade_despesa")
    specific_objective:                  CoercedStr   = Field(default=None, alias="objetivo_especifico")
    operation_typology:                  CoercedStr   = Field(default=None, alias="tipologia_operacao")
    covered_actions:                     CoercedStr   = Field(default=None, alias="acoes_abrangidas")
    intermediate_bodies:                 list[OrganismoIntermedio] = Field(default_factory=list, alias="organismos_intermedios")
    applicable_legislation:              CoercedLegislation = Field(default_factory=list, alias="legislacao_aplicavel")
    regulatory_documents:                list[DocumentoRegulamentacao] = Field(default_factory=list, alias="documentos_regulamentacao")
    target_technology_sectors:           CoercedList  = Field(default_factory=list, alias="setores_tecnologicos_alvo")
    application_submission:              CoercedStr   = Field(default=None, alias="submissao_candidaturas")
    beneficiary_eligibility_criteria:    CoercedList  = Field(default_factory=list, alias="criterios_elegibilidade_beneficiario")
    operation_eligibility_criteria:      CoercedList  = Field(default_factory=list, alias="criterios_elegibilidade_operacao")
    admissibility_conditions:            CoercedList  = Field(default_factory=list, alias="condicoes_admissibilidade")
    final_recipients:                    CoercedList  = Field(default_factory=list, alias="destinatarios_finais")
    dnsh_principle:                      CoercedStr   = Field(default=None, alias="principio_dnsh")
    commitment_requirements:             CoercedStr   = Field(default=None, alias="requisitos_compromisso")
    # P2 — território e execução
    total_allocation:                    CoercedFloat = Field(default=None, alias="dotacao_global")
    low_density_territories:             CoercedList  = Field(default_factory=list, alias="territorios_baixa_densidade")
    submission_limits:                   CoercedStr   = Field(default=None, alias="limites_submissao")
    absolute_execution_deadline:         CoercedStr   = Field(default=None, alias="data_limite_execucao_absoluta")
    financial_execution_targets:         CoercedList  = Field(default_factory=list, alias="metas_execucao_financeira")
    # P3 — financeiro
    minimum_investment:                  CoercedFloat = Field(default=None, alias="investimento_minimo")
    maximum_investment:                  CoercedFloat = Field(default=None, alias="investimento_maximo")
    required_self_financing_limit:       CoercedFloat = Field(default=None, alias="limite_autofinanciamento_exigido")
    state_aid_regime:                    CoercedStr   = Field(default=None, alias="regime_auxilio_estado")
    applicable_gber_article:             CoercedStr   = Field(default=None, alias="artigo_rgbc_aplicavel")
    contact:                             CoercedList  = Field(default_factory=list, alias="contacto")
    payment_methods:                     CoercedList  = Field(default_factory=list, alias="formas_pagamento")
    # P4 — avaliação
    project_selection_criteria:          CoercedList  = Field(default_factory=list, alias="criterios_selecao_projeto")
    # P5 — despesas e indicadores
    eligible_expenses:                   CoercedExpenseGroups = Field(default_factory=list, alias="despesas_elegiveis")
    ineligible_expenses:                 CoercedExpenseGroups = Field(default_factory=list, alias="despesas_nao_elegiveis")

    @field_validator("eligible_expenses", "ineligible_expenses", mode="after")
    @classmethod
    def _drop_empty_expense_groups(cls, v):
        # Remove grupos sem itens (ex: o shell "[{category:null, items:[]}]" que o LLM às vezes
        # devolve quando não encontra despesas). Assim, "vazio" volta a ser [] — o que faz o
        # rescue do P7 disparar (que ignora [] mas não [{...}]).
        return [g for g in v if g.items]
    output_indicators:                   list[Indicador] = Field(default_factory=list, alias="indicadores_realizacao")
    result_indicators:                   list[Indicador] = Field(default_factory=list, alias="indicadores_resultados")
    monitoring_indicators:               list[Indicador] = Field(default_factory=list, alias="indicadores_acompanhamento")
    beneficiary_obligations:             CoercedList  = Field(default_factory=list, alias="obrigacoes_beneficiarios")
    communication_obligations:           CoercedList  = Field(default_factory=list, alias="obrigacoes_comunicacao")
    # P6 — documentos
    application_documents:               list[ApplicationDocument] = Field(default_factory=list, alias="documentos_candidatura")
    # Campos adicionais de cobertura
    bonus_mechanisms:                    CoercedList  = Field(default_factory=list)
    dnsh_criteria:                       CoercedStr   = None
    # P7 — temas dos Anexos não capturados
    to_explore:                          CoercedList  = Field(default_factory=list, alias="aprofundar")


class BeneficiaryByAction(_Base):
    grant_code:  CoercedStr  = Field(default=None, alias="codigo_aviso")
    action_type: CoercedStr  = Field(default=None, alias="tipo_acao")
    entities:    CoercedList = Field(default_factory=list, alias="entidades")


class FinancingRate(_Base):
    grant_code:                 CoercedStr   = Field(default=None, alias="codigo_aviso")
    company_size:               CoercedStr   = Field(default=None, alias="dimensao_empresa")
    aid_regime:                 CoercedStr   = Field(default=None, alias="regime_auxilio")
    base_rate:                  CoercedStr   = Field(default=None, alias="taxa_base")
    regional_bonus:             CoercedStr   = Field(default=None, alias="majoracao_regional")
    max_global_rate:            CoercedStr   = Field(default=None, alias="taxa_maxima_global")
    minimis_accumulation_limit: CoercedFloat = Field(default=None, alias="limite_acumulacao_minimis")
    specific_condition:         CoercedStr   = Field(default=None, alias="condicao_especifica")


class ExpenseLimit(_Base):
    grant_code:                 CoercedStr   = Field(default=None, alias="codigo_aviso")
    expense_category:           CoercedStr   = Field(default=None, alias="rubrica_despesa")
    applicable_ocs_methodology: CoercedStr   = Field(default=None, alias="metodologia_ocs_aplicavel")
    max_absolute_value:         CoercedFloat = Field(default=None, alias="valor_maximo_absoluto")
    max_percentage_value:       CoercedFloat = Field(default=None, alias="valor_maximo_percentual")
    calculation_base:           CoercedStr   = Field(default=None, alias="base_calculo")
    specific_conditions:        CoercedStr   = Field(default=None, alias="condicoes_especifica")


class PenaltyTier(_Base):
    grade_range:   CoercedStr   = Field(default=None, alias="faixa_grau_cumprimento")
    reduction_pp:  CoercedFloat = Field(default=None, alias="reducao_pp")


class NonCompliancePenalty(_Base):
    grant_code:                          CoercedStr   = Field(default=None, alias="codigo_aviso")
    indicator_types:                     CoercedStr   = Field(default=None, alias="tipo_indicadores")
    compliance_grade_formula:            CoercedStr   = Field(default=None, alias="formula_grau_cumprimento")
    general_tolerance_threshold:         CoercedFloat = Field(default=None, alias="limiar_tolerancia_geral")
    low_density_tolerance_threshold:     CoercedFloat = Field(default=None, alias="limiar_tolerancia_baixa_densidade")
    reduction_per_percentage_point:      CoercedFloat = Field(default=None, alias="reducao_por_ponto_percentual")
    # Escalões discretizados: uma entrada por faixa de GC (ex: '] 70% - 65% ]' → 0.5 p.p.).
    penalty_tiers:                       list[PenaltyTier] = Field(default_factory=list, alias="escaloes_penalizacao")
    max_penalty_percentage:              CoercedFloat = Field(default=None, alias="penalizacao_maxima_percentual")
    financing_revocation_threshold:      CoercedFloat = Field(default=None, alias="limiar_revogacao_financiamento")
    rule_description:                    CoercedStr   = Field(default=None, alias="descricao_regra")


class EvaluationMethodology(_Base):
    grant_code:             CoercedStr   = Field(default=None, alias="codigo_aviso")
    project_merit_formula:  CoercedStr   = Field(default=None, alias="formula_merito_projeto")
    # Como se contabilizam os pontos: escala (ex: 1-5) + significado de cada nível +
    # regras de arredondamento. Distinto da fórmula (que pondera os critérios).
    scoring_scale:          CoercedStr   = Field(default=None, alias="escala_pontuacao")
    min_global_score:       CoercedFloat = Field(default=None, alias="pontuacao_minima_global")
    evaluation_criteria:    list[CriterioAvaliacao] = Field(default_factory=list, alias="criterios_avaliacao")
    tiebreaker_criteria:    CoercedList  = Field(default_factory=list, alias="criterios_desempate")


class Phase(_Base):
    phase_code:      CoercedStr = Field(default=None, alias="codigo_fase")
    grant_code:      CoercedStr = Field(default=None, alias="codigo_aviso")
    name:            CoercedStr = Field(default=None, alias="nome")
    start_date:      CoercedStr = Field(default=None, alias="data_inicio")
    end_date:        CoercedStr = Field(default=None, alias="data_fim")
    access_condition: CoercedStr = Field(default=None, alias="condicao_acesso")


class CoveredArea(_Base):
    area_code:       CoercedStr = Field(default=None, alias="codigo_area")
    grant_code:      CoercedStr = Field(default=None, alias="codigo_aviso")
    geographic_area: CoercedStr = Field(default=None, alias="area_geografica")


class TerritoryBudget(_Base):
    # Sub-registo GENÉRICO de repartição de uma dotação: `name` é o rótulo tal como o aviso o
    # dá (pode ser qualquer coisa — território, tipo de operação, etc.); `budget` fica no
    # formato legível do aviso (ex: "40.000.000,00").
    name:    CoercedStr = Field(default=None, alias="nome")
    budget:  CoercedStr = Field(default=None, alias="dotacao")


class PhaseArea(_Base):
    phase_code:          CoercedStr   = Field(default=None, alias="codigo_fase")
    area_code:           CoercedStr   = Field(default=None, alias="codigo_area")
    grant_code:          CoercedStr   = Field(default=None, alias="codigo_aviso")
    # Fundo/rubrica desta linha de dotação (ex: "FSE+", "FEDER", "Dotação Global"). Permite
    # distinguir a dotação por fundo (ex: 85%) da Dotação Global (100%) quando diferem.
    fund_name:           CoercedStr   = Field(default=None, alias="nome_fundo")
    budget_allocation:   CoercedFloat = Field(default=None, alias="dotacao_orcamental")
    max_financing_rate:  CoercedFloat = Field(default=None, alias="taxa_financiamento_maxima")
    # Repartição da dotação por território (ex: Baixa Densidade vs Outros Territórios).
    distribution:        list[TerritoryBudget] = Field(default_factory=list, alias="distribuicao")


# Parse functions — uma por prompt (P1–P6)

def parse_p1(raw: dict) -> tuple[dict, list[BeneficiaryByAction]]:
    from pydantic import TypeAdapter
    p1   = raw.get("Grant_Part1") or {}
    p1.pop("fundo_financiador", None)  # descartado — total_allocation vem do P2
    bpas = TypeAdapter(list[BeneficiaryByAction]).validate_python(raw.get("BeneficiaryByAction") or [])
    return p1, bpas


def parse_p2(raw: dict) -> tuple[dict, list[Phase], list[CoveredArea], list[PhaseArea]]:
    from pydantic import TypeAdapter
    grant_data = raw.get("Grant") or {}
    phases = TypeAdapter(list[Phase]).validate_python(raw.get("phases") or [])
    areas  = TypeAdapter(list[CoveredArea]).validate_python(raw.get("CoveredArea") or [])
    fa     = TypeAdapter(list[PhaseArea]).validate_python(raw.get("PhaseArea") or [])
    return grant_data, phases, areas, fa


def parse_p3(raw: dict) -> tuple[dict, list[FinancingRate]]:
    from pydantic import TypeAdapter
    grant_data = raw.get("Grant") or {}
    rates = TypeAdapter(list[FinancingRate]).validate_python(raw.get("FinancingRate") or [])
    return grant_data, rates


def parse_p4(raw: dict) -> tuple[dict, list[EvaluationMethodology]]:
    from pydantic import TypeAdapter
    grant_data    = raw.get("Grant") or {}
    methodologies = TypeAdapter(list[EvaluationMethodology]).validate_python(raw.get("EvaluationMethodology") or [])
    return grant_data, methodologies


def parse_p5(raw: dict) -> tuple[dict, list[ExpenseLimit], list[NonCompliancePenalty]]:
    from pydantic import TypeAdapter
    grant_data  = raw.get("Grant") or {}
    limits      = TypeAdapter(list[ExpenseLimit]).validate_python(raw.get("ExpenseLimit") or [])
    penalties   = TypeAdapter(list[NonCompliancePenalty]).validate_python(raw.get("NonCompliancePenalty") or [])
    return grant_data, limits, penalties


def parse_p6(raw: dict) -> dict:
    from pydantic import TypeAdapter
    docs = (raw.get("Grant") or {}).get("application_documents") or []
    return {"application_documents": TypeAdapter(list[ApplicationDocument]).validate_python(docs)}
