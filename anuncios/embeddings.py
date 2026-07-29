"""
Embeddings dos anúncios (Notice) para pesquisa semântica.

Mesma abordagem dos avisos: a OpenAI GERA o vetor, o pgvector GUARDA/PESQUISA no Postgres.
Reutiliza as funções genéricas de `match.embeddings` (embed/cosine/text_hash/semantic_search).
"""

from match import embeddings as emb


def notice_embedding_text(notice) -> str:
    """Texto representativo do anúncio para a comparação semântica."""
    parts = [notice.entity_name, notice.act_type, notice.description]
    parts += [str(c) for c in (notice.contract_types or [])]
    parts += [str(c) for c in (notice.cpvs or [])]
    return "\n".join(str(p) for p in parts if p)


def ensure_notice_embedding(notice):
    """Embedding do anúncio, da cache quando o texto não mudou; senão calcula e persiste.
    None se não houver API/erro."""
    text = notice_embedding_text(notice)
    h = emb.text_hash(text)
    if notice.activity_embedding is not None and notice.activity_embedding_hash == h:
        return notice.activity_embedding
    vec = emb.embed(text)
    if vec is not None:
        notice.activity_embedding = vec
        notice.activity_embedding_hash = h
        notice.save(update_fields=["activity_embedding", "activity_embedding_hash"])
    return vec


def search_notices(query: str, k: int = 10, only_active: bool = True) -> list:
    """Top-k anúncios semanticamente próximos de `query` (pgvector). [] se sem API."""
    from .models import Notice
    qs = Notice.objects.all()
    if only_active:
        qs = qs.filter(status=Notice.StatusChoices.ACTIVE)
    return emb.semantic_search(qs, query, field="activity_embedding", k=k)
