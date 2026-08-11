"""Peças partilhadas pelos emails-resumo enviados aos comerciais.

Os avisos (avisos/notifications.py) e os anúncios (anuncios/notifications.py) enviam digests
com a MESMA mecânica — quem recebe, como se formata dinheiro, e como se envia/regista — mas
com conteúdo próprio (assunto, secções, template). Só a mecânica vive aqui: assim uma
correção (ex: no arredondamento dos montantes) aplica-se de uma vez às duas, em vez de ter de
ser lembrada em dois sítios.

Deliberadamente NÃO abstrai a montagem do email em si: o assunto, as secções de texto e o
template são o que cada domínio tem de próprio, e forçá-los a um formato comum tornaria as
duas mais difíceis de ler e de evoluir do que a duplicação que se evitava.
"""

from django.conf import settings
from django.core.mail import send_mail

from users.models import UserProfile


def commercial_emails(*roles: str) -> list[str]:
    """Emails dos comerciais ATIVOS (com email preenchido) nos `roles` indicados.

    Os roles são passados por quem chama porque o alcance difere por domínio: os avisos
    interessam aos dois comerciais, os anúncios só ao commercial_public (o commercial_grants
    não tem sequer acesso a anúncios — ver anuncios/views.py).
    """
    return list(
        UserProfile.objects.filter(role__in=roles, user__is_active=True)
        .exclude(user__email="")
        .values_list("user__email", flat=True)
    )


def format_euros(value) -> str:
    """Montante em M€ / mil € / €.

    Uma casa decimal SÓ quando o valor não é redondo: evita "4 M€" para 3,5M sem produzir
    "3.0 M€" para um valor exato. Aceita Decimal (base_price dos anúncios) e float.
    """
    if value is None:
        return "N/D"
    value = float(value)

    def _round(n: float) -> str:
        return f"{n:.0f}" if n == int(n) else f"{n:.1f}"

    if value >= 1_000_000:
        return f"{_round(value / 1_000_000)} M€"
    if value >= 1_000:
        return f"{_round(value / 1_000)} mil €"
    return f"{value:.0f} €"


def send_digest(subject: str, body: str, html: str, recipients: list[str], logger) -> int:
    """Envia o digest (HTML + texto simples) e devolve o nº de destinatários.

    Best-effort por desenho: uma falha de SMTP não pode rebentar o scrape nem a edição que
    despoletou a notificação — fica no log e devolve 0.
    """
    if not recipients:
        return 0
    try:
        send_mail(
            subject, body, settings.DEFAULT_FROM_EMAIL, recipients,
            html_message=html, fail_silently=False,
        )
        logger.info("Notificação enviada a %d comercial(is): %s", len(recipients), subject)
        return len(recipients)
    except Exception:
        logger.exception("Falha ao enviar a notificação aos comerciais: %s", subject)
        return 0
