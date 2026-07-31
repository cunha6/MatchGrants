"""Notificação por email aos comerciais quando anúncios são adicionados/alterados.

Espelha avisos/notifications.py: o import (base.gov.pt) envia um email-resumo (digest) dos
anúncios processados (criados/atualizados), e uma edição manual envia um email do anúncio
alterado (só a lista de atualizados). Degrada graciosamente: sem destinatários ou em falha de
envio, apenas regista no log e nunca rebenta o fluxo que a chamou (import ou edição).

Anúncios não têm relação com o match nem com commercial_grants (ver anuncios/views.py
_VIEW_ROLES/_EDIT_ROLES) — só COMMERCIAL_PUBLIC (e admin, via listagem, mas não notificado
aqui, tal como em avisos) recebe este email.

O email é HTML (template anuncios/anuncios_publicados_email.html, com uma tabela por lista via
{% for %}) + uma versão em texto simples como alternativa. Genuinamente dinâmico a cada envio
— por isso usa o motor de templates do próprio Django em vez do pipeline React Email/render_emails
(esse só sabe gerar HTML estático, sem props reais; ver emails/anunciosPublicados.tsx para o
design de referência).
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from users.models import UserProfile

logger = logging.getLogger("anuncios.audit")


def commercial_emails() -> list[str]:
    """Emails dos utilizadores comerciais ativos (com email preenchido) com acesso a
    anúncios — só commercial_public (commercial_grants não tem acesso a anúncios)."""
    return list(
        UserProfile.objects.filter(
            role=UserProfile.COMMERCIAL_PUBLIC,
            user__is_active=True,
        )
        .exclude(user__email="")
        .values_list("user__email", flat=True)
    )


_TITLE_MAX_LEN = 140


def _notice_title(notice) -> str:
    """Texto identificador do anúncio na tabela do email — a descrição (o que de facto diz o
    que o anúncio é), truncada para caber na célula. Cai para entidade/número só se a descrição
    estiver vazia."""
    text = (notice.description or "").strip()
    if not text:
        return (notice.entity_name or notice.notice_number or "(sem descrição)").strip()
    return text if len(text) <= _TITLE_MAX_LEN else text[:_TITLE_MAX_LEN].rstrip() + "…"


def _notice_line(notice) -> str:
    title = _notice_title(notice)
    deadline = notice.proposal_deadline.strftime("%d/%m/%Y") if notice.proposal_deadline else "n/d"
    return f"- [{notice.notice_number or 's/n'}] {title} — prazo: {deadline}"


def _format_price(value) -> str:
    """Formata em M€/mil € — mesma lógica de avisos._format_allocation (1 casa decimal só
    quando não é um valor redondo)."""
    def _round(n: float) -> str:
        return f"{n:.0f}" if n == int(n) else f"{n:.1f}"

    if value is None:
        return "N/D"
    value = float(value)
    if value >= 1_000_000:
        return f"{_round(value / 1_000_000)} M€"
    if value >= 1_000:
        return f"{_round(value / 1_000)} mil €"
    return f"{value:.0f} €"


def _format_deadline(value) -> str:
    return value.strftime("%d/%m/%Y") if value else "N/D"


def _notice_row(notice) -> dict:
    """Uma linha da tabela do email (descrição/preço base/prazo/link). O link vai para a página
    de detalhe do anúncio no front-end — quem recebe este email já tem sessão (é comercial),
    tal como acontece nos avisos (ver avisos.notifications._grant_row)."""
    return {
        "titulo": _notice_title(notice),
        "preco": _format_price(notice.base_price),
        "prazo": _format_deadline(notice.proposal_deadline),
        "url": f"{settings.FRONTEND_URL}/anuncios/{notice.id}",
    }


def notify_notices(new_notices=None, updated_notices=None) -> int:
    """Envia UM email-resumo (HTML + texto simples) aos comerciais com anúncios criados e/ou
    atualizados. Devolve o nº de destinatários (0 se não houver anúncios, nenhum comercial com
    email, ou falha de envio). Best-effort: qualquer falha fica no log e não propaga.
    """
    new_notices = [n for n in (new_notices or []) if n is not None]
    updated_notices = [n for n in (updated_notices or []) if n is not None]
    if not new_notices and not updated_notices:
        return 0
    recipients = commercial_emails()
    if not recipients:
        logger.info("Notificação de anúncios ignorada: nenhum comercial com email.")
        return 0

    total = len(new_notices) + len(updated_notices)
    subject = f"[MatchGrants] {total} anúncio(s) novo(s)/atualizado(s)"

    body_parts = []
    if new_notices:
        body_parts.append("Novos anúncios:\n" + "\n".join(_notice_line(n) for n in new_notices))
    if updated_notices:
        body_parts.append(
            "Anúncios atualizados:\n" + "\n".join(_notice_line(n) for n in updated_notices))
    body = "\n\n".join(body_parts) + "\n\nConsulta a listagem para mais detalhes."

    html = render_to_string("anuncios/anuncios_publicados_email.html", {
        "new_notices": [_notice_row(n) for n in new_notices],
        "updated_notices": [_notice_row(n) for n in updated_notices],
    })

    try:
        send_mail(
            subject, body, settings.DEFAULT_FROM_EMAIL, recipients,
            html_message=html, fail_silently=False,
        )
        logger.info("Notificação enviada a %d comercial(is): %s", len(recipients), subject)
        return len(recipients)
    except Exception:
        logger.exception("Falha ao enviar a notificação de anúncios aos comerciais.")
        return 0
