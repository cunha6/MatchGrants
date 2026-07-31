"""Deteção de emails rejeitados (não-empresa) a partir de disposable_email_domains.json.

O ficheiro junta DOIS tipos de domínio a rejeitar: (1) um bloco inicial de fornecedores de
webmail genéricos/gratuitos (gmail.com, outlook.com, hotmail.com, yahoo.*, icloud.com,
sapo.pt...) — intencional: o gate de contacto do match quer um email de EMPRESA, não pessoal;
(2) a partir daí, a lista alfabética de domínios de email temporário/descartável (temp-mail).
Carregada UMA vez para um frozenset em memória (~124k domínios, ~2.5 MB; não justifica SQLite,
mesma lógica de match/nuts.py para o nuts.json). Usada no gate de contacto do match (ver
match.services._missing_contact_fields) para recusar emails "de lixo ou pessoais" antes de
revelar os resultados a quem não tem sessão.
"""

import json
from pathlib import Path

_DOMAINS_PATH = Path(__file__).resolve().parent.parent / "disposable_email_domains.json"


def _load() -> frozenset[str]:
    try:
        with open(_DOMAINS_PATH, encoding="utf-8") as f:
            domains = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return frozenset()
    return frozenset(d.strip().lower() for d in domains if d and d.strip())


# Carregado uma vez ao importar o módulo.
_DISPOSABLE_DOMAINS = _load()


def is_disposable_email(email: str) -> bool:
    """True se o domínio do email estiver na lista rejeitada — webmail genérico/gratuito OU
    temp-mail (ver docstring do módulo).

    Compara só o domínio (parte após o último '@'), sem validar o formato do email em si —
    isso é responsabilidade de quem chama (ver _missing_contact_fields, que só chega aqui
    depois de confirmar que o campo não está vazio)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1]
    return domain in _DISPOSABLE_DOMAINS
