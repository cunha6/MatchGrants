"""Chamadas à API do OpenAI específicas do pipeline de avisos (chunks + routing).

create_client/call_openai_text (genéricos, sem chunks) vivem em common/openai_client.py.
"""

import json
import logging
import time

from openai import AsyncOpenAI

from .prompts import ROUTER_SYSTEM, build_messages_from_chunks

logger = logging.getLogger(__name__)

# Modelos que não aceitam o parâmetro temperature
_MODELS_SEM_TEMP = ("o1", "o3", "o4", "gpt-5")


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
    started_at = time.time()
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
    if not any(model.startswith(model_prefix) for model_prefix in _MODELS_SEM_TEMP):
        kwargs["temperature"] = 0

    response = await client.chat.completions.create(**kwargs)

    usage = response.usage
    truncated = " [TRUNCADO]" if response.choices[0].finish_reason == "length" else ""
    logger.info(
        f"  [{label}] OK  "
        f"in={usage.prompt_tokens} out={usage.completion_tokens} tokens  "
        f"({time.time()-started_at:.1f}s){truncated}"
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
        f"CHUNK_ID: {chunk.get('chunk_index', id(chunk))}\n"
        f"HEADER: {chunk.get('title', '')[:80]}\n"
        f"TEXT: {chunk.get('text', '')[:200]}\n---"
        for chunk in chunks
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
