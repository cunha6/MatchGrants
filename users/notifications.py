"""Email de redefinição de password — ver users.service.request_password_reset.

O HTML (emails/html/resetPassword_email.html) é gerado a partir de emails/resetPassword.tsx
pelo management command `render_emails` (mesmo padrão do match/notifications.py). Esse .tsx
tem o botão com um href placeholder (https://example.com) — é essa string, já única no
ficheiro, que aqui se substitui pelo link real (com uid+token). Nada mais no HTML muda por
pedido, por isso não há necessidade de um motor de templates a sério.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger("users.audit")

_SUBJECT = "Alterar password"
_TEMPLATE_PATH = Path(settings.BASE_DIR) / "emails" / "html" / "resetPassword_email.html"
_PLACEHOLDER_URL = "https://example.com"


def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    """Envia o email de redefinição de password a `to_email`, com o link `reset_url`.
    Degrada graciosamente — uma falha de envio (ou o HTML em falta, ex: `render_emails`
    ainda não correu) fica só no log, nunca rebenta o pedido. Devolve True se enviado."""
    if not to_email:
        return False
    try:
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "Email de reset não enviado — falta %s (corre `python manage.py render_emails "
            "resetPassword`).", _TEMPLATE_PATH)
        return False
    html = html.replace(_PLACEHOLDER_URL, reset_url)
    try:
        send_mail(
            _SUBJECT, "", settings.DEFAULT_FROM_EMAIL, [to_email],
            html_message=html, fail_silently=False,
        )
        logger.info("Email de reset de password enviado a %s.", to_email)
        return True
    except Exception:
        logger.exception("Falha ao enviar o email de reset de password a %s.", to_email)
        return False
