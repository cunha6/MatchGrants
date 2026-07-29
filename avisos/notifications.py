"""Notificação por email aos utilizadores commercial quando avisos são adicionados/alterados.

Assim os comerciais ficam sempre a par dos avisos novos e das alterações — o scrape envia
um email-resumo (digest) dos avisos que processou, e uma edição manual envia um email do
aviso alterado. Degrada graciosamente: sem destinatários ou em falha de envio, apenas regista
no log e nunca rebenta o fluxo que a chamou (scrape ou edição).
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

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


def notify_grants(grants, action: str = "atualizados") -> int:
    """Envia UM email-resumo aos comerciais com os avisos `grants`. Devolve o nº de
    destinatários (0 se não houver comerciais, lista vazia, ou falha de envio).

    `action` descreve o que aconteceu ("adicionados ou atualizados", "alterado"…) e entra
    no assunto e no corpo. Best-effort: qualquer falha fica no log e não propaga.
    """
    grants = [g for g in grants if g is not None]
    if not grants:
        return 0
    recipients = commercial_emails()
    if not recipients:
        logger.info("Notificação de avisos ignorada: nenhum comercial com email.")
        return 0

    subject = f"[MatchGrants] {len(grants)} aviso(s) {action}"
    body = (
        f"Os seguintes avisos foram {action}:\n\n"
        + "\n".join(_grant_line(g) for g in grants)
        + "\n\nConsulta a listagem para mais detalhes."
    )
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
        logger.info("Notificação enviada a %d comercial(is): %s", len(recipients), subject)
        return len(recipients)
    except Exception:
        logger.exception("Falha ao enviar a notificação de avisos aos comerciais.")
        return 0
