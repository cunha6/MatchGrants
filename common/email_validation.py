"""Validação de email de EMPRESA — formato, domínio existente (DNS) e não descartável/webmail.

Usado tanto no gate de contacto do match (match/services.py) como no registo de conta
(users/service.py): a mesma regra de negócio nos dois sítios — "queremos um email real de
empresa, não pessoal/lixo/inventado" — vive aqui uma única vez.
"""

import logging

from email_validator import EmailNotValidError, EmailUndeliverableError, caching_resolver, validate_email

from .disposable_email import is_disposable_email

logger = logging.getLogger(__name__)

EMAIL_INVALID_LABEL = "Email inválido"
EMAIL_UNDELIVERABLE_LABEL = "O domínio deste email não existe — introduza um email profissional da empresa."

# Resolver DNS PARTILHADO entre pedidos (cache LRU + timeout curto). Sem isto cada validação
# usava o resolver por omissão do email_validator: timeout de 15s (podia prender um pedido
# nesse tempo todo num domínio inacessível, para uma verificação que normalmente demora
# <0.4s) e sem cache nenhuma (o mesmo domínio, testado várias vezes seguidas, repetia sempre
# a consulta DNS do zero).
_DNS_TIMEOUT = 3
_dns_resolver = caching_resolver(timeout=_DNS_TIMEOUT)


def email_error_label(value) -> str | None:
    """None se o email for válido; caso contrário a label do erro, ESPECÍFICA ao motivo (quem
    chama mostra-a tal e qual a quem preencheu o email, para orientar de forma diferente
    consoante o caso).

    Usa `email_validator` (não só uma verificação de formato) para verificar TAMBÉM que o
    domínio existe de facto (consulta MX/A por DNS) — sem isto um domínio inventado ou mal
    escrito (ex: "ana@empresa-que-nao-existe.pt") passava como "válido" só por ter o formato
    certo. Tolera valores não-string sem rebentar — o corpo do pedido pode ser JSON do
    cliente e trazer, por exemplo, {"email": 123}.

    Uma falha de REDE/DNS ao consultar o domínio (não confundir com o domínio genuinamente
    não ter registos) não bloqueia quem está do outro lado — degrada como o resto do projeto
    (nif.pt, OpenRouter): um problema transitório nosso não pode impedir um contacto real.

    O descartável/webmail (`is_disposable_email`) é verificado ANTES do DNS — é uma consulta
    local (JSON em memória), não uma chamada de rede: um domínio já sabido lixo nem precisa de
    esperar pelo DNS para dar erro.
    """
    if not isinstance(value, str) or not value.strip():
        return EMAIL_INVALID_LABEL
    if is_disposable_email(value):
        return EMAIL_INVALID_LABEL
    try:
        validate_email(value.strip(), check_deliverability=True, dns_resolver=_dns_resolver)
    except EmailUndeliverableError:
        return EMAIL_UNDELIVERABLE_LABEL
    except EmailNotValidError:
        return EMAIL_INVALID_LABEL
    except Exception as exc:  # noqa: BLE001 — falha de DNS/rede, não do email em si
        logger.warning("Verificação do domínio do email falhou (rede/DNS), a tratar como válido: %s", exc)
    return None
