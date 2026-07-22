"""Chamadas à API do OpenAI."""

import json
import logging
import os
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI

from .prompts import ROUTER_SYSTEM, build_messages_from_chunks

load_dotenv()

logger = logging.getLogger(__name__)

# Modelos que não aceitam o parâmetro temperature
_MODELS_SEM_TEMP = ("o1", "o3", "o4", "gpt-5")


def create_client() -> AsyncOpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key or api_key.startswith("sk-..."):
        raise RuntimeError("OPENAI_API_KEY não está definida no ficheiro .env")
    return AsyncOpenAI(api_key=api_key)


async def call_openai_text(
    client: AsyncOpenAI,
    system_prompt: str,
    user_content: str,
    label: str,
    model: str,
    max_tokens: int = 16_384,
) -> str:
    """Envia um prompt de texto livre e devolve a resposta em texto (não-JSON)."""
    t = time.time()
    logger.info(f"  [{label}] → {model}")
    kwargs: dict = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=max_tokens,
    )
    if not any(model.startswith(p) for p in _MODELS_SEM_TEMP):
        kwargs["temperature"] = 0
    response = await client.chat.completions.create(**kwargs)
    logger.info(f"  [{label}] OK ({time.time()-t:.1f}s)")
    return response.choices[0].message.content or ""


async def call_openai(
    client: AsyncOpenAI,
    system_prompt: str,
    chunks: list[dict],
    label: str,
    model: str,
    max_tokens: int = 16_384,
    extra_user: str = "",
) -> dict:
    """Envia chunks para o OpenAI e devolve o JSON parseado."""
    t = time.time()
    logger.info(f"  [{label}] {len(chunks)} secções → {model}")

    messages = build_messages_from_chunks(system_prompt, chunks)
    if extra_user:
        messages[-1]["content"] = extra_user + "\n\n---\n\n" + messages[-1]["content"]

    kwargs: dict = dict(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        max_completion_tokens=max_tokens,
    )
    if not any(model.startswith(p) for p in _MODELS_SEM_TEMP):
        kwargs["temperature"] = 0

    response = await client.chat.completions.create(**kwargs)

    usage = response.usage
    truncated = " [TRUNCADO]" if response.choices[0].finish_reason == "length" else ""
    logger.info(
        f"  [{label}] OK  "
        f"in={usage.prompt_tokens} out={usage.completion_tokens} tokens  "
        f"({time.time()-t:.1f}s){truncated}"
    )

    raw = response.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error(f"  [{label}] ERRO JSON: {exc}")
        return {}


async def classify_ambiguous_chunks(
    client: AsyncOpenAI,
    chunks: list[dict],
) -> dict[str, list[str]]:
    """
    Envia chunks sem categoria ao LLM numa única chamada para os classificar.
    Devolve {chunk_id -> [categorias]}.
    Em caso de erro devolve {} e o pipeline usa estes chunks como fallback para P1.
    """
    if not chunks:
        return {}

    parts = [
        f"CHUNK_ID: {c.get('chunk_index', id(c))}\n"
        f"HEADER: {c.get('title', '')[:80]}\n"
        f"TEXT: {c.get('text', '')[:200]}\n---"
        for c in chunks
    ]

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user",   "content": "\n".join(parts)},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=4000,
            temperature=0,
        )
        raw = response.choices[0].message.content or "{}"
        result = json.loads(raw)
        return {str(k): (v if isinstance(v, list) else [str(v)]) for k, v in result.items()}
    except Exception as exc:
        logger.warning(f"  [Router] Falhou: {exc}")
        return {}
