"""
Orquestração do scraping → recolha/classificação de documentos → consolidação de
alterações → extração IA → persistência.

Por aviso: recolhe TODOS os documentos, classifica-os, escolhe o canónico (republicação
consolidada > alteração > base), consolida diffs sobre o base quando necessário, e só
então corre o pipeline de extração uma única vez.
"""

import logging
import os
from datetime import date

from common.dates import parse_date as _parse_closing_date
from common.docling.converter import download_pdf, pdf_to_markdown, find_existing_document, text_is_invitation

from .scrape_compete import scrape_compete2030_web
from .scrape_portugal import scrape_portugal2030_web
from .scrape_prr import scrape_prr_web
from .IA.pipeline import run_pipeline, consolidate_markdowns
from .documents import (
    classify_document, order_documents, needs_consolidation, amendment_ordinal,
    BASE, REPUBLICATION, AMENDMENT, RECTIFICATION, ANNEX,
)
from .db_service import save_scraped_grant, save_ai_grant
from .models import Grant, GrantDocument
from .notifications import notify_grants

# Tipos de documento que entram na seleção do canónico / consolidação
_PRIMARY_TYPES = (BASE, REPUBLICATION, AMENDMENT, RECTIFICATION)

logger = logging.getLogger(__name__)


def _process_grant_safe(grant: dict, download_dir: str, source_label: str):
    """`_process_grant` isolado por aviso: um erro (OpenAI, PDF corrompido, validação)
    NÃO aborta o scrape inteiro — fica no log e o processamento continua no aviso seguinte."""
    try:
        return _process_grant(grant, download_dir, source_label)
    except Exception:
        logger.exception(
            "Erro ao processar o aviso %r (%s) — a continuar com os seguintes.",
            grant.get("grant_code") or grant.get("title"), source_label,
        )
        return None, False


def _candidate_documents(grant: dict) -> list[dict]:
    """Documentos do aviso relevantes para extração (base/republicação/alteração)."""
    out = []
    for document in grant.get("documentos") or []:
        url = document.get("url")
        if not url:
            continue
        doc_type = classify_document(document.get("nome", ""))
        if doc_type in _PRIMARY_TYPES:
            out.append({"name": document.get("nome", ""), "url": url, "type": doc_type})
    return out


def _annex_documents(grant: dict) -> list[dict]:
    """Anexos do aviso — NÃO são descarregados, só fica o nome + url para referência."""
    out = []
    for document in grant.get("documentos") or []:
        url = document.get("url")
        if not url:
            continue
        if classify_document(document.get("nome", "")) == ANNEX:
            out.append({"name": document.get("nome", ""), "url": url, "type": ANNEX})
    return out


def _latest_notice(grant: dict) -> tuple[str | None, str]:
    """URL + nome do aviso escolhido pelo scraper (leitura bottom-up + código + words_search)."""
    ln = grant.get("latest_notice")
    if not ln:
        return None, ""
    if isinstance(ln, dict):
        return ln.get("url"), ln.get("nome", "")
    return ln, ""  # PRR: latest_notice é uma string (url)


def _store_documents(grant_rec, other_documents: list[dict], canonical_url: str) -> None:
    grant_rec.documents.all().delete()
    GrantDocument.objects.bulk_create([
        GrantDocument(
            grant=grant_rec,
            doc_type=document["type"],
            name=document.get("name"),
            url=document.get("url", ""),
            ordinal=amendment_ordinal(document.get("name", "")),
            is_canonical=(document.get("url") == canonical_url),
        )
        for document in other_documents
    ])


def _discard_files(*paths: str | None) -> None:
    """Remove (best-effort) ficheiros de um aviso que decidimos NÃO processar (ex: convite)."""
    for file_path in paths:
        if not file_path:
            continue
        try:
            os.remove(file_path)
        except OSError:
            pass


def _is_in_db(canonical_url: str, code: str | None) -> bool:
    """O aviso já existe na BD? Procura pelo URL canónico ou pelo código (uma republicação
    traz um canonical_url novo para um grant_code já conhecido)."""
    return (Grant.objects.filter(scraping_url=canonical_url).exists()
            or (bool(code) and Grant.objects.filter(grant_code=code).exists()))


def _fetch_markdown(canonical_url: str, download_dir: str):
    """Descarrega o PDF canónico e converte-o em markdown.

    Devolve (path, markdown, md_path) ou None quando não há nada a processar: não-PDF, erro,
    ou CONVITE. Os convites são rejeitados em dois pontos — `reject_invitations` lê a
    "Natureza do aviso" no texto cru (barato, antes de converter), e o `text_is_invitation`
    é a rede de segurança para PDFs cujo texto cru saiu pobre e só ficou legível depois da
    conversão; nesse caso os ficheiros já escritos são apagados.
    """
    path = download_pdf(canonical_url, download_dir, reject_invitations=True)
    if not path:
        return None
    source_name = os.path.splitext(os.path.basename(path))[0]

    converted = pdf_to_markdown(path, download_dir)
    if not converted:
        return None
    canonical_md, md_path = converted

    if text_is_invitation(canonical_md):
        logger.info("[Convite] %s: Natureza=convite — ignorado", source_name)
        _discard_files(path, md_path)
        return None
    return path, canonical_md, md_path


def _consolidate(canonical_md: str, canonical_url: str, other_documents: list,
                 download_dir: str, source_name: str) -> tuple[str, bool]:
    """Se o documento canónico for um diff puro, aplica-o sobre o documento base.

    Devolve (markdown_final, needs_review). Sem base para aplicar o diff, devolve o próprio
    diff e needs_review=True — extrair de um diff isolado dá um aviso incompleto, que fica
    assim marcado para revisão humana em vez de entrar em silêncio.
    """
    if not needs_consolidation(canonical_md):
        return canonical_md, False

    base_doc = next(
        (document for document in other_documents
         if document["type"] in (BASE, REPUBLICATION) and document["url"] != canonical_url),
        None,
    )
    base_md = None
    if base_doc:
        bpath = download_pdf(base_doc["url"], download_dir)
        bconv = pdf_to_markdown(bpath, download_dir) if bpath else None
        base_md = bconv[0] if bconv else None

    if base_md:
        logger.info("[Consolidação] %s: aplicar diff sobre base", source_name)
        return consolidate_markdowns(base_md, [canonical_md]), False

    logger.warning("[Consolidação] %s: diff sem base — marcado needs_review", source_name)
    return canonical_md, True


def _store_canonical_and_related(grant_rec, canonical_name: str, canonical_url: str,
                                 other_documents: list, annexes: list, needs_review: bool) -> None:
    """Marca o needs_review e regista os documentos do aviso, com o canónico à cabeça."""
    grant_rec.needs_review = needs_review
    grant_rec.save(update_fields=["needs_review"])
    canonical_type = classify_document(canonical_name)
    if canonical_type not in (BASE, REPUBLICATION, AMENDMENT, RECTIFICATION):
        canonical_type = BASE
    canonical_rec = {"name": canonical_name, "url": canonical_url, "type": canonical_type}
    _store_documents(grant_rec, [canonical_rec] + other_documents + annexes, canonical_url)


def _process_grant(grant: dict, download_dir: str, source_label: str):
    """Processa um aviso: usa o aviso escolhido pelo scraper, consolida, extrai e persiste.

    Devolve (grant_rec, was_new) — was_new é sempre False nos ramos que devolvem
    grant_rec=None (nada foi criado/notificado)."""
    # Canónico = aviso que o scraper escolheu (bottom-up + código + words_search/reject).
    # NÃO reclassificamos por nome aqui — isso evita apanhar "Correspondência…Aviso…" e afins.
    canonical_url, canonical_name = _latest_notice(grant)
    if not canonical_url:
        return None, False

    # A EXTRAÇÃO é decidida pela PRESENÇA DO PDF na pasta:
    #  • PDF já na pasta  → NÃO descarrega, NÃO extrai, NÃO cria registo. Se o aviso já
    #    existir na BD, atualiza só os campos autoritativos do HTML (ex: prorrogação que muda
    #    a closing_date); se NÃO existir na BD, não faz nada (não o "coloca na BD").
    #  • PDF não na pasta → (re)descarrega, converte, verifica convite e extrai (a seguir).
    code = grant.get("grant_code")
    if find_existing_document(canonical_url, download_dir):
        if _is_in_db(canonical_url, code):
            save_scraped_grant({**grant, "url": canonical_url}, source_label)
        return None, False

    # was_new lê-se ANTES de qualquer escrita nesta função — é a única forma fiável de saber
    # se o save_ai_grant abaixo vai CRIAR um registo ou ATUALIZAR um já existente (republicação/
    # alteração de um grant_code conhecido, ou canonical_url novo de um aviso já na BD).
    was_new = not _is_in_db(canonical_url, code)

    # PDF NÃO está na pasta (aviso novo, PDF apagado, ou canónico novo/republicação):
    # descarrega + converte + verifica convite + extrai. O save_ai_grant faz match por
    # grant_code e ATUALIZA o registo existente (não duplica), reapontando o scraping_url.
    annexes = _annex_documents(grant)
    # Outros documentos primários (para consolidação de alterações e para registo)
    other_documents = order_documents([document for document in _candidate_documents(grant) if document["url"] != canonical_url])

    fetched = _fetch_markdown(canonical_url, download_dir)
    if fetched is None:
        return None, False
    path, canonical_md, md_path = fetched
    source_name = os.path.splitext(os.path.basename(path))[0]

    # É concurso → grava os dados do HTML correspondente (cria o registo).
    save_scraped_grant({**grant, "url": canonical_url}, source_label)

    final_md, needs_review = _consolidate(
        canonical_md, canonical_url, other_documents, download_dir, source_name)

    # annex_documents: TODOS os documentos da página do aviso — apenas nome + url
    # no JSON de saída (não são descarregados, ficam só para referência).
    extra = {"annex_documents": [
        {"name": document.get("nome", ""), "url": document["url"]}
        for document in grant.get("documentos") or []
        if document.get("url")
    ]}
    ai_data = run_pipeline(final_md, source_name, extra=extra)
    # Só chegamos aqui quando o PDF canónico ainda não estava em disco — aviso novo ou
    # canonical mudou (republicação/alteração mais recente). Em ambos os casos os dados
    # do PDF novo prevalecem; force_overwrite atualiza os escalares já preenchidos.
    # Os campos bloqueados do HTML continuam protegidos dentro de save_ai_grant.
    grant_rec = save_ai_grant(
        ai_data, scraping_url=canonical_url, pdf_path=path, markdown_path=md_path,
        force_overwrite=True,
    ) if ai_data else None

    if grant_rec:
        _store_canonical_and_related(
            grant_rec, canonical_name, canonical_url, other_documents, annexes, needs_review)
    return grant_rec, (was_new if grant_rec else False)


def deactivate_expired_grants(today: date | None = None) -> int:
    """Sincroniza Grant.active com a closing_date: terminados (data < hoje) → active=False;
    ainda a decorrer ou sem data legível → active=True (reativa se foi prorrogado). MANTÉM os
    ficheiros (PDF/markdown/JSON). Devolve o nº de registos cujo estado mudou."""
    today = today or date.today()
    to_update = []
    for grant in Grant.objects.only("id", "closing_date", "active"):
        closing = _parse_closing_date(grant.closing_date)
        want_active = not (closing is not None and closing < today)
        if grant.active != want_active:
            grant.active = want_active
            to_update.append(grant)
    if to_update:
        Grant.objects.bulk_update(to_update, ["active"])
        logger.info("[Avisos] %d aviso(s) mudaram de estado ativo/inativo (por data de fim).",
                    len(to_update))
    return len(to_update)


def _scrape(scraper, download_dir: str, source_label: str) -> list[dict]:
    """Corre um scraper de fonte, processa cada aviso (isolado por erros), desativa os
    terminados e envia UM email-resumo aos comerciais dos avisos processados — separado em
    criados vs. atualizados (ver _process_grant/notify_grants).
    Devolve os dicts de origem dos avisos processados (o que a rota expõe)."""
    scraped_grants = scraper()
    new_grants, created_records, updated_records = [], [], []
    for grant_dict in scraped_grants:
        grant_record, was_new = _process_grant_safe(grant_dict, download_dir, source_label)
        if grant_record:
            new_grants.append(grant_dict)
            (created_records if was_new else updated_records).append(grant_record)
    deactivate_expired_grants()
    if created_records or updated_records:
        notify_grants(created_records, updated_records)
    return new_grants


def scrape_compete() -> list[dict]:
    return _scrape(scrape_compete2030_web, "pdf_Avisos/compete", "compete")


def scrape_portugal() -> list[dict]:
    return _scrape(scrape_portugal2030_web, "pdf_Avisos/portugal", "portugal")


def scrape_prr() -> list[dict]:
    return _scrape(scrape_prr_web, "pdf_Avisos/prr", "prr")


def scrape_todos() -> dict:
    return {
        "compete": scrape_compete(),
        "portugal": scrape_portugal(),
        "prr": scrape_prr(),
    }
