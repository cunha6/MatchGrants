"""
Embeddings ESPECIALIZADOS do aviso (Grant): que textos representam cada dimensão, como se
persistem e como se comparam com o perfil da empresa.

Arquitetura — um embedding por TIPO (ver `GrantEmbedding.Type`), cada um com o seu builder
de texto registado em `_TEXT_BUILDERS`. Comparar dimensões separadamente evita que o sinal
setorial se dilua no texto geral do aviso.

ACRESCENTAR UM TIPO NOVO (ex: RECIPIENT) — sem alterar a estrutura da base de dados:
  1) juntar o valor a `GrantEmbedding.Type` (avisos/models.py);
  2) escrever `build_recipient_embedding_text(grant)` aqui;
  3) registá-lo em `_TEXT_BUILDERS`.
A persistência, a deteção de alterações e o batching funcionam automaticamente para ele.

A camada genérica (OpenAI + vetores) vive em `match/embeddings.py`.
"""

from avisos.models import GrantEmbedding

from . import embeddings as emb

# Pesos do score final. Constantes para calibração futura — o setor pesa mais porque é o
# sinal que melhor discrimina o domínio da empresa; o geral dá contexto (objetivos, ações,
# destinatários, região).
SECTOR_WEIGHT = 0.60
GENERAL_WEIGHT = 0.40

_WEIGHTS = {
    GrantEmbedding.Type.SECTOR: SECTOR_WEIGHT,
    GrantEmbedding.Type.GENERAL: GENERAL_WEIGHT,
}


# --- Builders de texto (um por tipo) ---------------------------------------

def build_general_embedding_text(grant) -> str:
    """Texto do conteúdo GERAL do aviso: o que financia, para quem e onde.

    Inclui título, objetivo, objetivo específico, tipologia, ações abrangidas, destinatários
    finais e regiões elegíveis. Fica de fora a burocracia (elegibilidade formal, documentos,
    prazos) — não descreve o que o aviso financia e só introduzia ruído.
    NOTA: os setores tecnológicos NÃO entram aqui — têm o seu próprio embedding (SECTOR).
    """
    parts = [
        grant.title, grant.objective, grant.specific_objective,
        grant.operation_typology, grant.covered_actions,
    ]
    parts += list(grant.final_recipients or [])
    if grant.eligible_regions:
        parts.append("Regiões elegíveis: " + ", ".join(str(region_name) for region_name in grant.eligible_regions))
    return "\n".join(str(part) for part in parts if part)


def build_sector_embedding_text(grant) -> str:
    """Texto SETORIAL: só o domínio tecnológico/económico do aviso.

    Usa exclusivamente `target_technology_sectors`. Isolado do resto, este sinal deixa de se
    diluir no texto geral e passa a discriminar avisos de nicho.
    Fallback: o título do aviso, quando não há setores declarados (é o texto mais próximo do
    domínio); sem ele, o aviso ficaria sem dimensão setorial nenhuma.
    """
    sectors = [str(sector).strip() for sector in (grant.target_technology_sectors or []) if sector]
    if sectors:
        return "\n".join(sectors)
    return (grant.title or "").strip()


# Registry tipo → builder. É a ÚNICA coisa a mexer para acrescentar um tipo novo.
_TEXT_BUILDERS = {
    GrantEmbedding.Type.GENERAL: build_general_embedding_text,
    GrantEmbedding.Type.SECTOR: build_sector_embedding_text,
}


def build_embedding_texts(grant) -> dict[str, str]:
    """{tipo: texto} para todos os tipos registados (só os que produzem texto não vazio)."""
    texts_by_type = {}
    for etype, builder in _TEXT_BUILDERS.items():
        text = (builder(grant) or "").strip()
        if text:
            texts_by_type[etype] = text
    return texts_by_type


# --- Persistência ----------------------------------------------------------

def pending_embeddings(grant, force: bool = False) -> list[tuple[str, str, str]]:
    """[(tipo, texto, hash)] dos embeddings deste aviso que precisam de ser (re)calculados.

    Um tipo entra na lista quando: ainda não existe, o texto mudou (hash diferente), ou foi
    gerado por outro modelo. `force=True` devolve todos. É esta função que garante que NÃO se
    chama a OpenAI para textos que não mudaram — usada tanto no save (um aviso) como no
    comando de backfill (em lote), sem duplicar a lógica.
    """
    texts = build_embedding_texts(grant)
    if not texts:
        return []
    existing = {existing_embedding.embedding_type: existing_embedding for existing_embedding in grant.embeddings.all()}
    pending = []
    for etype, text in texts.items():
        h = emb.text_hash(text)
        row = existing.get(etype)
        if force or row is None or row.text_hash != h or row.model != emb.MODEL:
            pending.append((etype, text, h))
    return pending


def store_embedding(grant, embedding_type: str, vector: list[float], text_hash: str) -> None:
    """Cria/atualiza o registo de um tipo (um por aviso+tipo)."""
    GrantEmbedding.objects.update_or_create(
        grant=grant, embedding_type=embedding_type,
        defaults={"embedding": vector, "text_hash": text_hash, "model": emb.MODEL},
    )


def save_grant_embeddings(grant, force: bool = False) -> dict[str, list[float]]:
    """Gera e grava os embeddings em falta/desatualizados do aviso. Devolve {tipo: vetor} do
    que foi (re)calculado ({} quando nada mudou — nesse caso NÃO há chamada à OpenAI).

    Os tipos pendentes vão numa ÚNICA chamada à API (embed_many).
    """
    pending = pending_embeddings(grant, force=force)
    if not pending:
        return {}
    vectors = emb.embed_many([text for _, text, _ in pending])
    saved = {}
    for (etype, _, h), vec in zip(pending, vectors):
        if vec is None:
            continue  # sem API/erro → fica por gerar, tenta-se na próxima
        store_embedding(grant, etype, vec, h)
        saved[etype] = vec
    return saved


def grant_vectors(grant) -> dict[str, list[float]]:
    """{tipo: vetor} já gravados do aviso. Só LÊ (usa os registos pré-carregados por
    prefetch_related no match — não gera nem chama a OpenAI)."""
    return {existing_embedding.embedding_type: existing_embedding.embedding for existing_embedding in grant.embeddings.all()}


# --- Similaridade ----------------------------------------------------------
def relevance(company_vectors: dict, grant_vectors: dict) -> tuple[float | None, float | None, float | None]:
    """(final_score, sector_similarity, general_similarity) entre a empresa e o aviso.

    FINAL = 0.60 × setorial + 0.40 × geral (ver SECTOR_WEIGHT/GENERAL_WEIGHT).

    Se só uma das dimensões existir (ex: a empresa não tem atividade descrita, ou o aviso
    ainda não tem embedding setorial), os pesos são RENORMALIZADOS sobre as disponíveis —
    assim um aviso não é penalizado por falta de dados (0.4×geral daria sempre <0.4, o que o
    empurraria injustamente para o fundo). None quando não há nenhuma dimensão comparável:
    o ranking cai para taxa+dotação.
    """
    sims = {}
    for etype in _WEIGHTS:
        cv, gv = company_vectors.get(etype), grant_vectors.get(etype)
        if cv is not None and gv is not None:
            sims[etype] = emb.cosine(cv, gv)

    sector = sims.get(GrantEmbedding.Type.SECTOR)
    general = sims.get(GrantEmbedding.Type.GENERAL)

    total_weight = sum(_WEIGHTS[embedding_type] for embedding_type in sims)
    if not total_weight:
        return None, sector, general
    final = sum(sims[embedding_type] * _WEIGHTS[embedding_type] for embedding_type in sims) / total_weight
    return final, sector, general