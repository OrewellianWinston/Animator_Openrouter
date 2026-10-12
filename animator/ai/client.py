"""Shared plumbing for AI calls: the OpenRouter client, retries and JSON parsing."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Type, TypeVar

import openai
from pydantic import BaseModel

from .settings import MAX_RETRIES, RETRY_DELAY_SEC

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_HEADERS = {"HTTP-Referer": "https://github.com/ashishjamarkattel/Animator", "X-Title": "Animator"}

T = TypeVar("T", bound=BaseModel)

# tries per structured request when the output is not valid JSON for the schema
STRUCTURED_ATTEMPTS = 3


@dataclass
class AIClient:
    """Blocking and async OpenAI-SDK clients, both pointed at OpenRouter."""
    sync: openai.OpenAI
    aio: openai.AsyncOpenAI


def make_client(api_key: str) -> AIClient:
    # max_retries=0: with_retry below owns the backoff policy
    common = dict(base_url=OPENROUTER_BASE_URL, api_key=api_key, max_retries=0, default_headers=_HEADERS)
    return AIClient(sync=openai.OpenAI(**common), aio=openai.AsyncOpenAI(**common))


def is_retryable(exc: Exception) -> bool:
    """Rate limits, server errors and dropped connections are worth retrying."""
    if isinstance(exc, (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError)):
        return True
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


def with_retry(call):
    """Run a blocking AI call, backing off on retryable errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return call()
        except Exception as exc:
            if attempt == MAX_RETRIES or not is_retryable(exc):
                raise
            logger.warning("AI call failed (%s); retry %s/%s", exc, attempt, MAX_RETRIES)
            time.sleep(RETRY_DELAY_SEC * attempt)


async def with_retry_async(call):
    """Await an AI call made by `call()`, backing off on retryable errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await call()
        except Exception as exc:
            if attempt == MAX_RETRIES or not is_retryable(exc):
                raise
            logger.warning("AI call failed (%s); retry %s/%s", exc, attempt, MAX_RETRIES)
            await asyncio.sleep(RETRY_DELAY_SEC * attempt)


# Some providers (e.g. Alibaba) only honour JSON output when a message mentions "json".
JSON_SYSTEM_MESSAGE = {"role": "system", "content": "Reply with a single JSON object that matches the given schema."}


def json_response_format(name: str, model: Type[BaseModel]) -> dict:
    """OpenRouter `response_format` asking for JSON matching a pydantic model."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": False, "schema": inline_refs(model.model_json_schema())},
    }


def inline_refs(schema: dict) -> dict:
    """Replace local `$ref`s with their definitions; some providers reject `$defs`."""
    defs = schema.get("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].rsplit("/", 1)[-1]]
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return resolve({**target, **extra})
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def message_text(response) -> str:
    """Text of the first choice, or '' when the model returned nothing."""
    if not getattr(response, "choices", None):
        return ""
    return response.choices[0].message.content or ""


def parse_json_model(text: str, model: Type[T], prepare: Optional[Callable[[Any], Any]] = None) -> T:
    """Validate model output as JSON, tolerating a ```json fence around it.

    `prepare` may repair the decoded JSON (e.g. clamp numbers) before validation.
    """
    cleaned = text.strip()
    fenced = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    data = json.loads(cleaned)
    return model.model_validate(prepare(data) if prepare else data)


def request_structured(call, model: Type[T], what: str, prepare=None) -> T:
    """Run `call()` (a blocking chat completion), parse JSON; retry once on bad output."""
    raw = ""
    for _ in range(STRUCTURED_ATTEMPTS):
        raw = message_text(with_retry(call))
        try:
            return parse_json_model(raw, model, prepare)
        except ValueError as exc:  # JSONDecodeError and pydantic ValidationError
            logger.warning("%s returned invalid JSON (%s); retrying", what, type(exc).__name__)
    raise RuntimeError(f"{what} returned invalid JSON. raw: {raw[:500]}")


async def request_structured_async(call, model: Type[T], what: str, prepare=None) -> T:
    """Async twin of request_structured; `call()` returns an awaitable completion."""
    raw = ""
    for _ in range(STRUCTURED_ATTEMPTS):
        raw = message_text(await with_retry_async(call))
        try:
            return parse_json_model(raw, model, prepare)
        except ValueError as exc:
            logger.warning("%s returned invalid JSON (%s); retrying", what, type(exc).__name__)
    raise RuntimeError(f"{what} returned invalid JSON. raw: {raw[:500]}")
