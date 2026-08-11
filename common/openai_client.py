"""Cliente OpenAI genérico — sem formatação de chunks nem taxonomia de categorias.

Partilhado entre avisos e anuncios (e qualquer app futura que precise de chamar o OpenAI
com um prompt de texto livre). Chamadas que dependem de estrutura de chunks/routing
específica de um domínio ficam no próprio app (ver avisos/IA/openai_client.py).
"""

import logging
import os
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI

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
    json_mode: bool = False,
) -> str:
    """Envia um prompt de texto livre (sem chunks) e devolve a resposta em texto.

    Com json_mode=True pede ao OpenAI para responder em JSON (response_format), mas a
    resposta continua a ser devolvida como string — quem chama é que faz json.loads
    e valida a forma esperada (não há aqui um esquema fixo, ao contrário de call_openai
    no avisos/IA, que devolve um dict já a partir de chunks estruturados).
    """
    started_at = time.time()
    logger.info(f"  [{label}] → {model}")
    kwargs: dict = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=max_tokens,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if not any(model.startswith(model_prefix) for model_prefix in _MODELS_SEM_TEMP):
        kwargs["temperature"] = 0
    response = await client.chat.completions.create(**kwargs)
    logger.info(f"  [{label}] OK ({time.time()-started_at:.1f}s)")
    return response.choices[0].message.content or ""
