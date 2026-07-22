"""
Camada GENÉRICA de embeddings: falar com a API da OpenAI e comparar vetores.

Responsabilidade única — não sabe o que é um aviso nem um anúncio. É a base partilhada por:
  • `match/grant_embeddings.py`  → embeddings especializados dos avisos (GENERAL, SECTOR, …)
  • `anuncios/embeddings.py`     → embeddings dos anúncios

Degradação graciosa: sem OPENAI_API_KEY (ou em falha de rede/API), as funções devolvem None
e quem chama segue sem semântica (o ranking cai para os critérios não-semânticos).
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


def generate_embedding(text: str) -> list[float] | None:
    """Vetor de um texto com o modelo `MODEL`. None se vazio / sem API / erro."""
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


# Alias retrocompatível: `anuncios/embeddings.py` e código existente chamam `embed`.
embed = generate_embedding


def embed_many(texts: list[str]) -> list[list[float] | None]:
    """Embeddings de vários textos numa ÚNICA chamada à API (evita chamadas repetidas).
    Entradas vazias → None na posição respetiva; a ordem do resultado espelha a entrada."""
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
    """Hash estável do texto embebido — deteta quando o texto mudou e força recálculo."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def semantic_search(queryset, query_text: str, field: str = "activity_embedding", k: int = 10):
    """Top-k registos do `queryset` semanticamente próximos de `query_text`, ordenados por
    distância de cosseno CALCULADA NO POSTGRES (pgvector). Serve qualquer modelo com uma
    coluna VectorField. Devolve [] se não houver API para embutir a query.
    """
    from pgvector.django import CosineDistance
    qvec = generate_embedding(query_text)
    if qvec is None:
        return []
    return list(
        queryset.filter(**{f"{field}__isnull": False})
        .annotate(distance=CosineDistance(field, qvec))
        .order_by("distance")[:k]
    )
