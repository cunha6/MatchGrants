"""Notificação por email aos utilizadores commercial quando avisos são adicionados/alterados.

Assim os comerciais ficam sempre a par dos avisos novos e das alterações — o scrape envia
um email-resumo (digest) dos avisos que processou (separado em criados/atualizados), e uma
edição manual envia um email do aviso alterado (só a lista de atualizados). Degrada
graciosamente: sem destinatários ou em falha de envio, apenas regista no log e nunca rebenta
o fluxo que a chamou (scrape ou edição).

O email é HTML (template avisos/avisos_abertos_email.html, com uma tabela por lista via
{% for %}) + uma versão em texto simples como alternativa. Ao contrário do welcome/reset
(estáticos, pré-renderizados do .tsx), este é genuinamente dinâmico a cada envio — por isso
usa o motor de templates do próprio Django em vez do pipeline React Email/render_emails.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from users.models import UserProfile

logger = logging.getLogger("avisos.audit")


def commercial_emails() -> list[str]:
    """Emails dos utilizadores comerciais ativos (com email preenchido) com acesso a avisos —
    commercial_grants (especialista) e commercial_public (acumula avisos+anúncios)."""
    return list(
        UserProfile.objects.filter(
            role__in=(UserProfile.COMMERCIAL_GRANTS, UserProfile.COMMERCIAL_PUBLIC),
            user__is_active=True,
        )
        .exclude(user__email="")
        .values_list("user__email", flat=True)
    )


def _grant_line(grant) -> str:
    code = grant.grant_code or "(sem código)"
    title = (grant.title or "").strip()
    closing = grant.closing_date or "n/d"
    return f"- [{code}] {title} — data final: {closing}"


def _format_allocation(value: float | None) -> str:
    """Formata em M€/mil € — 1 casa decimal só quando não é um valor redondo (evita
    "4 M€" para 3.5M, mas também evita "3.0 M€" para um valor exato)."""
    def _round(n: float) -> str:
        return f"{n:.0f}" if n == int(n) else f"{n:.1f}"

    if value is None:
        return "N/D"
    if value >= 1_000_000:
        return f"{_round(value / 1_000_000)} M€"
    if value >= 1_000:
        return f"{_round(value / 1_000)} mil €"
    return f"{value:.0f} €"


def _format_rate(value: float | None) -> str:
    return "N/D" if value is None else f"{value:.0f}%"


def _grant_row(grant) -> dict:
    """Uma linha da tabela do email (título/dotação/taxa/link). O link vai para a página do
    aviso no front-end — quem recebe este email já tem sessão (é comercial)."""
    # Import local: avisos.views importa notify_grants deste módulo — importar _financing_rate
    # ao nível do módulo criava um ciclo. Só _financing_rate é reaproveitada (cálculo único).
    from .views import _financing_rate
    return {
        "titulo": grant.title or grant.grant_code or "(sem título)",
        "dotacao": _format_allocation(grant.total_allocation),
        "taxa": _format_rate(_financing_rate(grant)),
        "url": f"{settings.FRONTEND_URL}/avisos/{grant.id}",
    }


def notify_grants(new_grants=None, updated_grants=None) -> int:
    """Envia UM email-resumo (HTML + texto simples) aos comerciais com avisos criados e/ou
    atualizados. Devolve o nº de destinatários (0 se não houver avisos, nenhum comercial com
    email, ou falha de envio). Best-effort: qualquer falha fica no log e não propaga.
    """
    new_grants = [g for g in (new_grants or []) if g is not None]
    updated_grants = [g for g in (updated_grants or []) if g is not None]
    if not new_grants and not updated_grants:
        return 0
    recipients = commercial_emails()
    if not recipients:
        logger.info("Notificação de avisos ignorada: nenhum comercial com email.")
        return 0

    total = len(new_grants) + len(updated_grants)
    subject = f"[MatchGrants] {total} aviso(s) novo(s)/atualizado(s)"

    body_parts = []
    if new_grants:
        body_parts.append("Novos avisos:\n" + "\n".join(_grant_line(g) for g in new_grants))
    if updated_grants:
        body_parts.append(
            "Avisos atualizados:\n" + "\n".join(_grant_line(g) for g in updated_grants))
    body = "\n\n".join(body_parts) + "\n\nConsulta a listagem para mais detalhes."

    html = render_to_string("avisos/avisos_abertos_email.html", {
        "new_grants": [_grant_row(g) for g in new_grants],
        "updated_grants": [_grant_row(g) for g in updated_grants],
    })

    try:
        send_mail(
            subject, body, settings.DEFAULT_FROM_EMAIL, recipients,
            html_message=html, fail_silently=False,
        )
        logger.info("Notificação enviada a %d comercial(is): %s", len(recipients), subject)
        return len(recipients)
    except Exception:
        logger.exception("Falha ao enviar a notificação de avisos aos comerciais.")
        return 0
