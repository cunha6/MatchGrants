"""
Orquestração do scraping → recolha/classificação de documentos → consolidação de
alterações → extração IA → persistência.

Por aviso: recolhe TODOS os documentos, classifica-os, escolhe o canónico (republicação
consolidada > alteração > base), consolida diffs sobre o base quando necessário, e só
então corre o pipeline de extração uma única vez.
"""

import os
import re
from datetime import date, datetime

from .scrape_compete import scrape_compete2030_web
from .scrape_portugal import scrape_portugal2030_web
from .scrape_prr import scrape_prr_web
from .Docling.converter import download_pdf, pdf_to_markdown, find_existing_document, text_is_invitation
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


def _discard_files(*paths: str | None) -> None:
    """Remove (best-effort) ficheiros de um aviso que decidimos NÃO processar (ex: convite)."""
    for p in paths:
        if not p:
            continue
        try:
            os.remove(p)
        except OSError:
            pass


def _process_grant(grant: dict, download_dir: str, source_label: str):
    """Processa um aviso: usa o aviso escolhido pelo scraper, consolida, extrai e persiste."""
    # Canónico = aviso que o scraper escolheu (bottom-up + código + words_search/reject).
    # NÃO reclassificamos por nome aqui — isso evita apanhar "Correspondência…Aviso…" e afins.
    canonical_url, canonical_name = _latest_notice(grant)
    if not canonical_url:
        return None

    # A EXTRAÇÃO é decidida pela PRESENÇA DO PDF na pasta:
    #  • PDF já na pasta  → NÃO descarrega, NÃO extrai, NÃO cria registo. Se o aviso já
    #    existir na BD, atualiza só os campos autoritativos do HTML (ex: prorrogação que muda
    #    a closing_date); se NÃO existir na BD, não faz nada (não o "coloca na BD").
    #  • PDF não na pasta → (re)descarrega, converte, verifica convite e extrai (a seguir).
    if find_existing_document(canonical_url, download_dir):
        code = grant.get("grant_code")
        in_db = (Grant.objects.filter(scraping_url=canonical_url).exists()
                 or (bool(code) and Grant.objects.filter(grant_code=code).exists()))
        if in_db:
            save_scraped_grant({**grant, "url": canonical_url}, source_label)
        return None

    # PDF NÃO está na pasta (aviso novo, PDF apagado, ou canónico novo/republicação):
    # descarrega + converte + verifica convite + extrai. O save_ai_grant faz match por
    # grant_code e ATUALIZA o registo existente (não duplica), reapontando o scraping_url.
    annexes = _annex_documents(grant)
    # Outros documentos primários (para consolidação de alterações e para registo)
    ordered = order_documents([d for d in _candidate_documents(grant) if d["url"] != canonical_url])

    # Download efetivo (PDF novo). O `reject_invitations` faz a verificação de convite
    # BARATA e primeiro: lê a "Natureza do aviso" no texto do PDF (pypdf) ANTES de converter —
    # convites detetáveis aqui devolvem None e nem chegam a ser convertidos nem gravados.
    # não-PDF / erro / convite → return sem criar qualquer registo.
    path = download_pdf(canonical_url, download_dir, reject_invitations=True)
    if not path:
        return None
    source_name = os.path.splitext(os.path.basename(path))[0]

    converted = pdf_to_markdown(path, download_dir)
    if not converted:
        return None
    canonical_md, md_path = converted

    # Rede de segurança: PDFs onde o texto cru saiu pobre (ex: capa mal extraída) e a Natureza
    # só ficou legível depois da conversão. Se for convite, ignoramos e apagamos os ficheiros.
    if text_is_invitation(canonical_md):
        print(f"  [Convite] {source_name}: Natureza=convite — ignorado")
        _discard_files(path, md_path)
        return None

    # É concurso → grava os dados do HTML correspondente (cria o registo).
    save_scraped_grant({**grant, "url": canonical_url}, source_label)

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

    # annex_documents: TODOS os documentos da página do aviso — apenas nome + url
    # no JSON de saída (não são descarregados, ficam só para referência).
    extra = {"annex_documents": [
        {"name": d.get("nome", ""), "url": d["url"]}
        for d in grant.get("documentos") or []
        if d.get("url")
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
        grant_rec.needs_review = needs_review
        grant_rec.save(update_fields=["needs_review"])
        canonical_type = classify_document(canonical_name)
        if canonical_type not in (BASE, REPUBLICATION, AMENDMENT, RECTIFICATION):
            canonical_type = BASE
        canonical_rec = {"name": canonical_name, "url": canonical_url, "type": canonical_type}
        _store_documents(grant_rec, [canonical_rec] + ordered + annexes, canonical_url)
    return grant_rec


def _parse_closing_date(text) -> date | None:
    """Interpreta a closing_date (texto livre) numa date, ou None se não der.
    Aceita ISO (2026-09-30[T…]), DD/MM/AAAA e AAAA/MM/DD (com '/' ou '-')."""
    if not text:
        return None
    s = str(text).strip()
    try:
        return datetime.fromisoformat(s[:19].replace("Z", "")).date()
    except ValueError:
        pass
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", s)   # DD/MM/AAAA
    if m:
        d, mo, y = (int(x) for x in m.groups())
    else:
        m = re.search(r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b", s)   # AAAA/MM/DD
        if not m:
            return None
        y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def deactivate_expired_grants(today: date | None = None) -> int:
    """Sincroniza Grant.active com a closing_date: terminados (data < hoje) → active=False;
    ainda a decorrer ou sem data legível → active=True (reativa se foi prorrogado). MANTÉM os
    ficheiros (PDF/markdown/JSON). Devolve o nº de registos cujo estado mudou."""
    today = today or date.today()
    changed = 0
    for grant in Grant.objects.only("id", "closing_date", "active"):
        closing = _parse_closing_date(grant.closing_date)
        want_active = not (closing is not None and closing < today)
        if grant.active != want_active:
            grant.active = want_active
            grant.save(update_fields=["active"])
            changed += 1
    if changed:
        print(f"  [Avisos] {changed} aviso(s) mudaram de estado ativo/inativo (por data de fim).")
    return changed


def scrape_compete() -> list[dict]:
    all_data = scrape_compete2030_web()
    new_grants = [g for g in all_data if _process_grant(g, "pdf_Avisos/compete", "compete")]
    deactivate_expired_grants()
    return new_grants


def scrape_portugal() -> list[dict]:
    all_data = scrape_portugal2030_web()
    new_grants = [g for g in all_data if _process_grant(g, "pdf_Avisos/portugal", "portugal")]
    deactivate_expired_grants()
    return new_grants


def scrape_prr() -> list[dict]:
    all_data = scrape_prr_web()
    new_grants = [g for g in all_data if _process_grant(g, "pdf_Avisos/prr", "prr")]
    deactivate_expired_grants()
    return new_grants


def scrape_todos() -> dict:
    return {
        "compete": scrape_compete(),
        "portugal": scrape_portugal(),
        "prr": scrape_prr(),
    }
