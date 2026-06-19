"""
Orquestração do scraping → recolha/classificação de documentos → consolidação de
alterações → extração IA → persistência.

Por aviso: recolhe TODOS os documentos, classifica-os, escolhe o canónico (republicação
consolidada > alteração > base), consolida diffs sobre o base quando necessário, e só
então corre o pipeline de extração uma única vez.
"""

import os

from .scrape_compete import scrape_compete2030_web
from .scrape_portugal import scrape_portugal2030_web
from .scrape_prr import scrape_prr_web
from .Docling.converter import download_pdf, pdf_to_markdown, find_existing_document
from .IA.pipeline import run_pipeline, consolidate_markdowns
from .documents import (
    classify_document, order_documents, needs_consolidation, amendment_ordinal,
    BASE, REPUBLICATION, AMENDMENT, RECTIFICATION, ANNEX,
)
from .db_service import save_scraped_grant, save_ai_grant
from .models import Grant, GrantDocument

# Tipos de documento que entram na seleção do canónico / consolidação
_PRIMARY_TYPES = (BASE, REPUBLICATION, AMENDMENT, RECTIFICATION)


def _candidate_documents(grant: dict) -> list[dict]:
    """Documentos do aviso relevantes para extração (base/republicação/alteração)."""
    out = []
    for d in grant.get("documentos") or []:
        url = d.get("url")
        if not url:
            continue
        doc_type = classify_document(d.get("nome", ""))
        if doc_type in _PRIMARY_TYPES:
            out.append({"name": d.get("nome", ""), "url": url, "type": doc_type})
    return out


def _annex_documents(grant: dict) -> list[dict]:
    """Anexos do aviso — NÃO são descarregados, só fica o nome + url para referência."""
    out = []
    for d in grant.get("documentos") or []:
        url = d.get("url")
        if not url:
            continue
        if classify_document(d.get("nome", "")) == ANNEX:
            out.append({"name": d.get("nome", ""), "url": url, "type": ANNEX})
    return out


def _latest_notice(grant: dict) -> tuple[str | None, str]:
    """URL + nome do aviso escolhido pelo scraper (leitura bottom-up + código + words_search)."""
    ln = grant.get("latest_notice")
    if not ln:
        return None, ""
    if isinstance(ln, dict):
        return ln.get("url"), ln.get("nome", "")
    return ln, ""  # PRR: latest_notice é uma string (url)


def _store_documents(grant_rec, ordered: list[dict], canonical_url: str) -> None:
    grant_rec.documents.all().delete()
    GrantDocument.objects.bulk_create([
        GrantDocument(
            grant=grant_rec,
            doc_type=d["type"],
            name=d.get("name"),
            url=d.get("url", ""),
            ordinal=amendment_ordinal(d.get("name", "")),
            is_canonical=(d.get("url") == canonical_url),
        )
        for d in ordered
    ])


def _process_grant(grant: dict, download_dir: str, source_label: str):
    """Processa um aviso: usa o aviso escolhido pelo scraper, consolida, extrai e persiste."""
    # Canónico = aviso que o scraper escolheu (bottom-up + código + words_search/reject).
    # NÃO reclassificamos por nome aqui — isso evita apanhar "Correspondência…Aviso…" e afins.
    canonical_url, canonical_name = _latest_notice(grant)
    if not canonical_url:
        return None

    # Já processado na BD (este canónico exato + ai_processed): o aviso já existe.
    # Atualiza apenas os campos autoritativos do HTML (ex: prorrogação que muda a
    # closing_date) e salta a extração. NÃO cria registo novo — só atualiza o existente.
    if Grant.objects.filter(scraping_url=canonical_url, ai_processed=True).exists():
        save_scraped_grant({**grant, "url": canonical_url}, source_label)
        return None

    # PDF já está na pasta mas não está na BD → por enquanto, ignora (não cria registo).
    # Só registamos avisos cujo PDF seja efetivamente descarregado nesta passagem.
    if find_existing_document(canonical_url, download_dir):
        return None

    annexes = _annex_documents(grant)
    # Outros documentos primários (para consolidação de alterações e para registo)
    ordered = order_documents([d for d in _candidate_documents(grant) if d["url"] != canonical_url])

    # Download efetivo (PDF novo, não está na pasta). Verificação de convite incluída;
    # convite / não-PDF / erro → return sem criar qualquer registo.
    path = download_pdf(canonical_url, download_dir, reject_invitations=True)
    if not path:
        return None
    source_name = os.path.splitext(os.path.basename(path))[0]

    # PDF descarregado → agora grava os dados do HTML correspondente (cria o registo).
    save_scraped_grant({**grant, "url": canonical_url}, source_label)

    converted = pdf_to_markdown(path, download_dir)
    if not converted:
        return None
    canonical_md, md_path = converted

    # Consolidação: se o canónico for um diff puro, aplicar sobre o documento base
    final_md = canonical_md
    needs_review = False
    if needs_consolidation(canonical_md):
        base_doc = next(
            (d for d in ordered
             if d["type"] in (BASE, REPUBLICATION) and d["url"] != canonical_url),
            None,
        )
        base_md = None
        if base_doc:
            bpath = download_pdf(base_doc["url"], download_dir)
            bconv = pdf_to_markdown(bpath, download_dir) if bpath else None
            base_md = bconv[0] if bconv else None
        if base_md:
            print(f"  [Consolidação] {source_name}: aplicar diff sobre base")
            final_md = consolidate_markdowns(base_md, [canonical_md])
        else:
            needs_review = True
            print(f"  [Consolidação] {source_name}: diff sem base — marcado needs_review")

    # Anexos: não descarregados — apenas nome + url no JSON de saída
    extra = {"annex_documents": [{"name": a["name"], "url": a["url"]} for a in annexes]}
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
        grant_rec.needs_review = needs_review
        grant_rec.save(update_fields=["needs_review"])
        canonical_type = classify_document(canonical_name)
        if canonical_type not in (BASE, REPUBLICATION, AMENDMENT, RECTIFICATION):
            canonical_type = BASE
        canonical_rec = {"name": canonical_name, "url": canonical_url, "type": canonical_type}
        _store_documents(grant_rec, [canonical_rec] + ordered + annexes, canonical_url)
    return grant_rec


def scrape_compete() -> list[dict]:
    all_data = scrape_compete2030_web()
    new_grants = [g for g in all_data if _process_grant(g, "pdf_Avisos/compete", "compete")]
    return new_grants


def scrape_portugal() -> list[dict]:
    all_data = scrape_portugal2030_web()
    new_grants = [g for g in all_data if _process_grant(g, "pdf_Avisos/portugal", "portugal")]
    return new_grants


def scrape_prr() -> list[dict]:
    all_data = scrape_prr_web()
    new_grants = [g for g in all_data if _process_grant(g, "pdf_Avisos/prr", "prr")]
    return new_grants


def scrape_todos() -> dict:
    return {
        "compete": scrape_compete(),
        "portugal": scrape_portugal(),
        "prr": scrape_prr(),
    }
