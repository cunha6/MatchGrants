"""
Embeddings OpenAI para a procura semântica por atividade da empresa.

O embedding de cada aviso é caro de calcular repetidamente, por isso fica em CACHE no
próprio Grant (campos `activity_embedding` + `activity_embedding_hash`): calcula-se uma vez
e reutiliza-se; se o texto do aviso mudar, o hash deixa de bater e recalcula-se. No match,
só se calcula 1 embedding novo — o da atividade do cliente.

Degradação graciosa: sem OPENAI_API_KEY (ou em falha de rede), `embed`/`embed_many`
devolvem None e o ranking simplesmente não usa relevância semântica (cai para taxa+dotação).
"""

import hashlib
import os

import numpy as np
from openai import OpenAI

MODEL = "text-embedding-3-small"
_MAX_CHARS = 8000  # ~limite de tokens do modelo, com folga

_client: OpenAI | None = None


def _get_client() -> OpenAI | None:
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY", "")
        if not key or key.startswith("sk-..."):
            return None
        _client = OpenAI(api_key=key)
    return _client


def embed(text: str) -> list[float] | None:
    """Embedding de um texto (ou None se vazio / sem API / erro)."""
    text = (text or "").strip()
    if not text:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        resp = client.embeddings.create(model=MODEL, input=text[:_MAX_CHARS])
        return resp.data[0].embedding
    except Exception:
        return None


def embed_many(texts: list[str]) -> list[list[float] | None]:
    """Embeddings em lote (1 chamada). Entradas vazias → None na posição respetiva."""
    idx = [i for i, t in enumerate(texts) if (t or "").strip()]
    out: list[list[float] | None] = [None] * len(texts)
    if not idx:
        return out
    client = _get_client()
    if client is None:
        return out
    try:
        resp = client.embeddings.create(
            model=MODEL, input=[texts[i][:_MAX_CHARS] for i in idx]
        )
        for pos, item in zip(idx, resp.data):
            out[pos] = item.embedding
    except Exception:
        return out
    return out


def cosine(a, b) -> float:
    """Similaridade de cosseno (0..1 para embeddings OpenAI); 0.0 se algum for vazio.

    Aceita listas ou numpy arrays (o VectorField do pgvector devolve numpy array) — usa
    comparações explícitas (`is None`/`.size`) para não cair no 'truth value of an array is
    ambiguous'."""
    if a is None or b is None:
        return 0.0
    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if va.size == 0 or vb.size == 0:
        return 0.0
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def text_hash(text: str) -> str:
    """Hash estável do texto que foi embebido — deteta quando o aviso mudou."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def semantic_search(queryset, query_text: str, field: str = "activity_embedding", k: int = 10):
    """Top-k registos do `queryset` semanticamente próximos de `query_text`, ordenados por
    distância de cosseno CALCULADA NO POSTGRES (pgvector). Serve avisos e anúncios (qualquer
    modelo com uma coluna VectorField). Devolve [] se não houver API para embutir a query.
    """
    from pgvector.django import CosineDistance
    qvec = embed(query_text)
    if qvec is None:
        return []
    return list(
        queryset.filter(**{f"{field}__isnull": False})
        .annotate(distance=CosineDistance(field, qvec))
        .order_by("distance")[:k]
    )


def grant_embedding_text(grant) -> str:
    """Texto representativo do aviso para a comparação semântica com o perfil do cliente.

    Junta o que o aviso FINANCIA (título, objetivo, objetivo específico, tipologia, ações,
    setores-alvo) + a sua abrangência (regiões elegíveis), para casar com a atividade E a
    localização do cliente. Fica de fora a burocracia (elegibilidade formal, documentos).
    """
    parts = [
        grant.title, grant.objective, grant.specific_objective,
        grant.operation_typology, grant.covered_actions,
    ]
    parts += list(grant.target_technology_sectors or [])
    if grant.eligible_regions:
        parts.append("Regiões elegíveis: " + ", ".join(str(r) for r in grant.eligible_regions))
    return "\n".join(str(p) for p in parts if p)


def ensure_grant_embedding(grant):
    """Embedding do aviso, da cache (Grant.activity_embedding) quando o texto não mudou;
    senão calcula-o e persiste. Devolve o vetor ou None (sem API/erro).

    Chamado ao GRAVAR o aviso (db_service) para que fique sempre pronto — assim o match não
    depende de correr o comando `embed_grants` nem de calcular embeddings à la carte.
    """
    text = grant_embedding_text(grant)
    h = text_hash(text)
    if grant.activity_embedding is not None and grant.activity_embedding_hash == h:
        return grant.activity_embedding
    vec = embed(text)
    if vec is not None:
        grant.activity_embedding = vec
        grant.activity_embedding_hash = h
        grant.save(update_fields=["activity_embedding", "activity_embedding_hash"])
    return vec
