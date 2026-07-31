"""Email de boas-vindas enviado a quem preenche o contacto (email/nome/função) no pop-up do
match sem login — ver NifMatchingService.create_or_update_viewer (services.py).

O HTML (emails/html/welcome_email.html) é gerado a partir de emails/welcome.tsx pelo
management command `render_emails` — esse .tsx é a fonte de verdade; este ficheiro só lê o
HTML já pronto (ficheiro estático, sem tags Django — por isso leitura direta, não
render_to_string). O Django em runtime não precisa de Node.js.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger("match.audit")

_SUBJECT = "Bem-vindo à Aliados"
_TEMPLATE_PATH = Path(settings.BASE_DIR) / "emails" / "html" / "welcome_email.html"


def send_welcome_email(to_email: str) -> bool:
    """Envia o email de boas-vindas a `to_email`. Degrada graciosamente — uma falha de envio
    (ou o HTML em falta, ex: `render_emails` ainda não correu) fica só no log, nunca rebenta
    o fluxo do match. Devolve True se enviado, False se não."""
    if not to_email:
        return False
    try:
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "Email de boas-vindas não enviado — falta %s (corre `python manage.py "
            "render_emails welcome`).", _TEMPLATE_PATH)
        return False
    try:
        send_mail(
            _SUBJECT, "", settings.DEFAULT_FROM_EMAIL, [to_email],
            html_message=html, fail_silently=False,
        )
        logger.info("Email de boas-vindas enviado a %s.", to_email)
        return True
    except Exception:
        logger.exception("Falha ao enviar o email de boas-vindas a %s.", to_email)
        return False
