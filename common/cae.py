"""Utilitários de CAE partilhados entre a extração (lado do aviso) e o match (lado do cliente).

O padrão CAE de um aviso é um prefixo com wildcard ('*'): '64***' (Divisão), '651**' (Grupo),
'6512*' (Classe), '65124' (Subclasse, exato). A elegibilidade compara por PREFIXO. Estas
funções extraem o prefixo de um padrão (para a tabela GrantCae) e geram todos os prefixos de um
CAE de cliente (para o prefiltro SQL do match: `WHERE prefix IN (prefixos do cliente)`).
"""


def cae_pattern_prefix(pattern) -> str | None:
    """Prefixo numérico de um padrão CAE do aviso ('64***' → '64', '65124' → '65124').
    None se o padrão for mal-formado (não começa por dígitos)."""
    s = str(pattern).strip()
    star = s.find("*")
    prefix = s if star == -1 else s[:star]
    return prefix if prefix.isdigit() else None


def cae_all_prefixes(cae) -> set[str]:
    """Todos os prefixos de um CAE de cliente: '65124' → {'6','65','651','6512','65124'}.

    Um aviso com padrão '65***' (prefixo '65') é candidato ao cliente '65124' porque '65' está
    neste conjunto. É o que torna a regra do prefixo indexável em SQL (`prefix IN (...)`)."""
    s = str(cae).strip()
    return {s[:i] for i in range(1, len(s) + 1) if s[:i].isdigit()}
