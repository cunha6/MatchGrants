"""Detalhe IA do caderno de encargos de um anúncio: PDF → markdown (cache) → OpenAI → JSON.

Reutiliza o Docling e o cliente OpenAI genéricos (common/docling, common/openai_client) —
o mesmo pipeline de conversão usado nos avisos, mas aqui com um prompt fixo e um esquema
de resposta próprio dos anúncios: um único pedido de texto livre (sem chunking/routing),
ao contrário do pipeline de 7 prompts do avisos/IA.

Cache persistido no próprio Notice para não repetir trabalho (nem custo — a conversão
Docling é CPU, o pedido ao OpenAI é pago):
  - specifications_markdown_path: o caderno de encargos já convertido para markdown.
  - specifications_description/specifications_evaluation/specifications_observations:
    a última resposta da IA, já validada — um campo por chave do esquema (em vez de um
    JSON opaco único), para ficarem legíveis/pesquisáveis na base de dados.
  - specifications_ai_status: PENDING/GENERATING/DONE/ERROR — para a view não bloquear à
    espera do OpenAI (ver generate_detail_async).

Dois pontos de entrada:
  - generate_detail(notice, force=False) — SÍNCRONO/bloqueante, devolve o detalhe já
    pronto (cacheado ou gerado agora). Usar fora de um request HTTP (scripts, comandos).
  - generate_detail_async(notice, force=False) — NÃO bloqueia: se já não houver nada em
    cache, arranca a geração numa thread de fundo e devolve logo ("generating"). É o que
    a view usa — o pedido HTTP não fica à espera da conversão Docling + chamada ao OpenAI.
"""

import asyncio
import json
import logging
import threading
from datetime import timedelta

from django.db import connection
from django.db.models import Q
from django.utils import timezone
from pydantic import BaseModel, Field, ValidationError

from common.docling.converter import pdf_to_markdown
from common.files import safe_media_path
from common.openai_client import call_openai_text, create_client

from .models import Notice

logger = logging.getLogger(__name__)

# Se uma geração ficar "presa" em GENERATING mais do que isto (ex: o worker do gunicorn
# reiniciou a meio), uma chamada seguinte relança-a em vez de ficar bloqueada para sempre.
# Generosa face à duração normal (Docling + 1 chamada ao OpenAI é questão de segundos).
_STALE_GENERATING_AFTER = timedelta(minutes=5)

# Pasta onde ficam os cadernos de encargos descarregados (ver anuncios/views.py).
_ANUNCIOS_PDF_DIR = "pdf_Anuncios"
_MODEL = "gpt-5-mini-2025-08-07"

SYSTEM_PROMPT = """\
És um assistente que lê o caderno de encargos de um procedimento de contratação pública
portuguesa (em markdown, convertido do PDF original) e extrai informação factual do
próprio documento — nunca a tua opinião ou uma avaliação subjetiva do concurso.

IMPORTANTE — NÃO REPETIR dados que a aplicação já mostra separadamente a partir do
registo oficial do anúncio (GET /anuncios/<id>/): entidade adjudicante, NIF da entidade,
preço base, data de publicação, prazo/data-limite de propostas, tipo de procedimento,
tipo de ato, CPVs, lotes, tipos de contrato, critérios ambientais. Mesmo que estes valores
apareçam no caderno de encargos, NÃO os repitas em nenhum dos três campos abaixo — foca-te
apenas no que é exclusivo do caderno de encargos e não está nesses campos.

Responde APENAS com um objeto JSON, sem texto antes ou depois, exatamente com esta forma:

{
  "descricao_detalhada": "string",
  "avaliacao": "string",
  "observacoes": ["string", ...]
}

Regras para cada campo:

- descricao_detalhada: descrição detalhada do OBJETO do procedimento (o que está a ser
  contratado, âmbito, principais prestações/obrigações) tal como consta do caderno de
  encargos — mais desenvolvida do que um simples título ou resumo de uma linha. NÃO
  menciones aqui o preço base, a entidade adjudicante, o NIF, datas/prazos ou o tipo de
  procedimento — esses campos já existem noutro sítio da aplicação.

- avaliacao: identifica os critérios de adjudicação e fatores de avaliação da proposta,
  e devolve APENAS o texto final descrito abaixo — sem explicações adicionais, sem
  markdown, só a frase.

  Procura secções com títulos como "Critério de adjudicação", "Avaliação das propostas",
  "Fatores de avaliação", "Modelo de avaliação", "Critérios de seleção" ou "Proposta
  economicamente mais vantajosa".

  Identifica:
  * O tipo de critério de adjudicação: Preço mais baixo / Multifator (Proposta
    economicamente mais vantajosa) / Monofator / Outro.
  * Todos os fatores e subfatores de avaliação, com os nomes EXATAMENTE como aparecem
    no documento.
  * A ponderação (%) de cada fator, quando existir.

  Formato do texto a devolver (uma única frase, "[Tipo] – [fator] ([peso]) + ...");
  mantém os nomes dos fatores tal como no documento; se existirem subfatores, inclui-os
  entre parênteses a seguir ao fator; se não existirem percentagens, indica só os
  fatores identificados, sem inventar pesos. Exemplo:
  "Multifator – Proposta economicamente mais vantajosa: Preço (30%) + Experiência
  Técnica da Equipa (35%) + Qualidade Técnica da Proposta (35%)"

  Se o critério for exclusivamente o preço, devolve exatamente:
  "Monofator – Preço mais baixo (100%)"
  Se não for possível identificar critérios de avaliação no documento, devolve
  exatamente:
  "Não foi possível identificar os critérios de avaliação no documento."

- observacoes: LISTA de notas relevantes para quem vai preparar uma proposta e que não
  cabem nos campos anteriores — uma string por nota, cada uma curta e autónoma (ex:
  "Visita ao local obrigatória.", "Caução de 5% do valor da adjudicação."). Exemplos de
  temas: exigência de alvará/certificado, visita ao local obrigatória, caução/garantia
  exigida, condições de pagamento, prazo de execução/entrega (NÃO o prazo de entrega de
  propostas — esse já está disponível separadamente). Lista vazia [] se não houver nada
  relevante a assinalar.

Usa sempre português. Não deixes nenhum campo em falta — usa string vazia ("") em
descricao_detalhada, lista vazia ([]) em observacoes, e a frase-sentinela indicada acima
em avaliacao, quando a informação não existir no documento.
"""


class TenderAIDetail(BaseModel):
    descricao_detalhada: str = ""
    # Frase única (ex: "Multifator – Preço (30%) + Qualidade (70%)"), não um objeto — ver
    # a regra de "avaliacao" no SYSTEM_PROMPT.
    avaliacao: str = ""
    observacoes: list[str] = Field(default_factory=list)


class SpecificationsNotFound(Exception):
    """Não há caderno de encargos descarregado (ou convertível) para este anúncio."""


def _coerce_avaliacao(value) -> str:
    """notice.specifications_evaluation pode ter ficado gravado no formato ANTIGO (um
    objeto {criterios, formula}, de antes de avaliacao passar a ser uma frase única) —
    tratado como vazio em vez de rebentar a validação; um refresh=true regenera-o já no
    formato novo."""
    return value if isinstance(value, str) else ""


def _get_markdown(notice: Notice) -> str:
    """Devolve o markdown do caderno de encargos, convertendo e persistindo o caminho na
    primeira vez. Reconverte se o ficheiro markdown já gravado tiver desaparecido."""
    if notice.specifications_markdown_path:
        existing = safe_media_path(notice.specifications_markdown_path, "output/markdown")
        if existing:
            with open(existing, encoding="utf-8") as md_file:
                return md_file.read()

    pdf_path = safe_media_path(notice.specifications_path, _ANUNCIOS_PDF_DIR)
    if not pdf_path:
        raise SpecificationsNotFound("Caderno de encargos não disponível para este anúncio.")

    converted = pdf_to_markdown(pdf_path, _ANUNCIOS_PDF_DIR)
    if not converted:
        raise SpecificationsNotFound("Não foi possível converter o caderno de encargos.")
    markdown, md_path = converted

    notice.specifications_markdown_path = md_path
    notice.save(update_fields=["specifications_markdown_path"])
    return markdown


async def _ask_ai(markdown: str) -> dict:
    client = create_client()
    raw = await call_openai_text(
        client, SYSTEM_PROMPT, markdown, "Detalhe IA (anúncio)", _MODEL, json_mode=True,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("[specifications_ai] resposta não é JSON válido: %s", raw[:300])
        return {}


def generate_detail(notice: Notice, force: bool = False) -> TenderAIDetail:
    """Ponto de entrada. Devolve o detalhe IA cacheado nos três campos do Notice
    (specifications_description/evaluation/observations) a menos que ainda não exista ou
    `force=True` — nesse caso converte/reconverte o markdown (cache próprio) e chama o
    OpenAI, gravando o resultado antes de devolver.

    Levanta SpecificationsNotFound se o anúncio não tiver caderno de encargos descarregado
    nem convertido.
    """
    if notice.specifications_description and not force:
        return TenderAIDetail(
            descricao_detalhada=notice.specifications_description,
            avaliacao=_coerce_avaliacao(notice.specifications_evaluation),
            observacoes=notice.specifications_observations or [],
        )

    markdown = _get_markdown(notice)
    raw = asyncio.run(_ask_ai(markdown))
    try:
        detail = TenderAIDetail.model_validate(raw)
    except ValidationError as exc:
        logger.error("[specifications_ai] JSON fora do esquema esperado: %s", exc)
        detail = TenderAIDetail()

    notice.specifications_description = detail.descricao_detalhada
    notice.specifications_evaluation = detail.avaliacao
    notice.specifications_observations = detail.observacoes
    notice.save(update_fields=[
        "specifications_description", "specifications_evaluation", "specifications_observations",
    ])
    return detail


def _has_specifications(notice: Notice) -> bool:
    """Verificação RÁPIDA (sem converter): há markdown já em cache, ou pelo menos um PDF
    para converter mais tarde na thread de fundo? Usada por generate_detail_async para
    devolver 404 já (sem arrancar nada) quando não há nenhum documento."""
    if notice.specifications_markdown_path and safe_media_path(
        notice.specifications_markdown_path, "output/markdown",
    ):
        return True
    return bool(safe_media_path(notice.specifications_path, _ANUNCIOS_PDF_DIR))


def generate_detail_async(notice: Notice, force: bool = False) -> tuple[str, TenderAIDetail | None]:
    """Versão NÃO bloqueante de generate_detail — é o que a view usa, para o pedido HTTP
    não ficar à espera da conversão Docling + chamada ao OpenAI.

    Devolve (status, detail):
      - ("done", detail) — já havia um detalhe cacheado (e force=False): devolve-o já.
      - ("generating", None) — arrancou agora a geração em background (ou já estava a
        decorrer uma). Repetir a chamada mais tarde devolve "done" quando estiver pronta
        — é assim que reabrir o anúncio mostra o que a IA gerou entretanto.

    Levanta SpecificationsNotFound se o anúncio não tiver nenhum caderno de encargos (nem
    PDF nem markdown já convertido) — não há nada para arrancar em background.
    """
    if not force and notice.specifications_ai_status == Notice.AiStatusChoices.DONE:
        return "done", TenderAIDetail(
            descricao_detalhada=notice.specifications_description,
            avaliacao=_coerce_avaliacao(notice.specifications_evaluation),
            observacoes=notice.specifications_observations or [],
        )

    if not _has_specifications(notice):
        raise SpecificationsNotFound("Caderno de encargos não disponível para este anúncio.")

    # Só arranca uma geração nova se não houver já uma em curso — evita duas threads a
    # gerar (e a pagar) o mesmo detalhe ao mesmo tempo. UPDATE atómico: só quem conseguir
    # mudar o estado é que arranca a thread; um pedido concorrente só encontra "generating".
    # Exceção: uma GENERATING "presa" há mais de _STALE_GENERATING_AFTER (ex: o worker
    # reiniciou a meio) volta a ficar reclamável, senão o anúncio ficava bloqueado para
    # sempre.
    stale_cutoff = timezone.now() - _STALE_GENERATING_AFTER
    claimed = Notice.objects.filter(pk=notice.pk).filter(
        ~Q(specifications_ai_status=Notice.AiStatusChoices.GENERATING) | Q(updated_at__lt=stale_cutoff)
    ).update(specifications_ai_status=Notice.AiStatusChoices.GENERATING, updated_at=timezone.now())
    if claimed:
        threading.Thread(
            target=_run_and_store_in_thread, args=(notice.pk, force), daemon=True,
        ).start()
    return "generating", None


def _run_and_store(notice_id: int, force: bool) -> None:
    """O trabalho em si: gera o detalhe e grava o estado final (DONE/ERROR). Alvo da
    thread de fundo, mas também chamável a direito nos testes (sem thread real, sem
    tocar na ligação à BD — ver _run_and_store_in_thread)."""
    try:
        notice = Notice.objects.get(pk=notice_id)
        generate_detail(notice, force=force)
        Notice.objects.filter(pk=notice_id).update(
            specifications_ai_status=Notice.AiStatusChoices.DONE)
    except Exception:
        logger.exception(
            "[specifications_ai] Falhou a geração em background do anúncio %s", notice_id)
        Notice.objects.filter(pk=notice_id).update(
            specifications_ai_status=Notice.AiStatusChoices.ERROR)


def _run_and_store_in_thread(notice_id: int, force: bool) -> None:
    """Alvo REAL da thread de fundo: corre _run_and_store e fecha a ligação à BD desta
    thread no fim — cada thread nova abre a sua própria ligação Django; sem fechar, fica
    pendurada até o processo terminar."""
    try:
        _run_and_store(notice_id, force)
    finally:
        connection.close()
