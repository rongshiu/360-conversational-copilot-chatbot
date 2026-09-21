# app/service/llm_service.py
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from functools import lru_cache
from typing import Any, AsyncIterator, Callable, Protocol, Type, TypeVar

from google import genai
from google.genai import types
from google.genai.types import HttpOptions
from openai import OpenAI
from pydantic import BaseModel

from app.core import settings
from app.service.copilot_usage_service import (
    _now_ms,
    extract_gemini_usage,
    get_usage_tracker,
)
from app.service.mlflow_observability import mlflow_span, redact_text, safe_dict

T = TypeVar("T", bound=BaseModel)


class LLMService(Protocol):
    model: str

    async def generate_text(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        ...

    async def stream_text(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        system_instruction: str | None = None,
    ) -> AsyncIterator[str]:
        ...

    async def generate_json(
        self,
        prompt: str,
        schema: Type[T],
        *,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> T:
        ...


# Both provider SDKs (google-genai sync client and openai sync client) expose
# blocking, non-streaming calls. The graph runs on an asyncio event loop, so
# these are offloaded to a worker thread to avoid blocking every concurrent
# request. asyncio.to_thread propagates the current contextvars (usage tracker,
# MLflow span context) into the worker.
_JSON_INSTRUCTION = (
    "\n\nReturn ONLY a valid JSON object that matches the required schema. "
    "Do not use markdown fences. Do not include explanation text."
)


async def _stream_sync_iterator(
    *,
    iterator_factory: Callable[[], Any],
    extract_text: Callable[[Any], str],
    extract_usage: Callable[[Any], dict[str, int]] | None,
    span_name: str,
    model: str,
) -> AsyncIterator[str]:
    """
    Convert a blocking SDK stream iterator into an async token stream.

    Both OpenAI's sync client and google-genai's sync client expose blocking
    stream iterators. FastAPI's StreamingResponse needs an async iterator, so
    this bridges the blocking SDK stream through an asyncio.Queue.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    tracker = get_usage_tracker()
    started_ms = _now_ms()
    latest_usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }

    def put(kind: str, value: Any) -> None:
        future = asyncio.run_coroutine_threadsafe(queue.put((kind, value)), loop)
        future.result()

    def worker() -> None:
        nonlocal latest_usage

        try:
            for item in iterator_factory():
                if extract_usage is not None:
                    usage = extract_usage(item)
                    if usage.get("total_tokens", 0) > 0:
                        latest_usage = usage

                text = extract_text(item)
                if text:
                    put("delta", text)

            put("done", None)

        except Exception as exc:
            put("error", exc)

    with mlflow_span(
        f"llm.{span_name}",
        inputs={"streaming": True},
        attributes={"model": model, "span_category": "llm"},
    ) as span:
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        output_chunks: list[str] = []

        try:
            while True:
                kind, value = await queue.get()

                if kind == "delta":
                    output_chunks.append(value)
                    yield value
                    continue

                if kind == "error":
                    raise value

                if kind == "done":
                    break

        finally:
            latency_ms = max(0, _now_ms() - started_ms)

            if tracker is not None:
                tracker.add_llm_call(
                    name=span_name,
                    latency_ms=latency_ms,
                    input_tokens=latest_usage.get("input_tokens", 0),
                    output_tokens=latest_usage.get("output_tokens", 0),
                    total_tokens=latest_usage.get("total_tokens", 0),
                    model=model,
                )

            if span is not None:
                span.set_outputs(
                    safe_dict(
                        {
                            "text_preview": redact_text("".join(output_chunks), limit=1000),
                            "latency_ms": latency_ms,
                            "input_tokens": latest_usage.get("input_tokens", 0),
                            "output_tokens": latest_usage.get("output_tokens", 0),
                            "total_tokens": latest_usage.get("total_tokens", 0),
                        },
                        limit=1000,
                    )
                )


# ---------------------------------------------------------------------------
# Vertex context caching for static system instructions.
#
# Agent prompts are split into a large STATIC rule block (identical across every
# request and user) and a small dynamic suffix. The static block is sent as the
# model's system_instruction. For Vertex/Gemini we register that block once as a
# CachedContent and reference it by name, so the rule tokens are billed at the
# cached rate instead of fresh on every call.
#
# Failures are non-fatal: if a block is too small to cache, the project lacks
# caching quota, or a cache expires, we transparently fall back to sending the
# system_instruction inline.
# ---------------------------------------------------------------------------
_CACHE_REGISTRY: dict[str, str] = {}
_CACHE_DISABLED: set[str] = set()
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = "3600s"


def _cache_key(model: str, system_instruction: str) -> str:
    digest = hashlib.sha256(f"{model}::{system_instruction}".encode("utf-8")).hexdigest()
    return f"{model}:{digest}"


def _get_or_create_cache(client: Any, model: str, system_instruction: str) -> str | None:
    """Return a reusable CachedContent name for this static block, or None.

    None means "cache unavailable for this block" and the caller should send the
    system_instruction inline.
    """
    key = _cache_key(model, system_instruction)

    if key in _CACHE_DISABLED:
        return None

    name = _CACHE_REGISTRY.get(key)
    if name:
        return name

    with _CACHE_LOCK:
        name = _CACHE_REGISTRY.get(key)
        if name:
            return name
        if key in _CACHE_DISABLED:
            return None

        try:
            cache = client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    system_instruction=system_instruction,
                    ttl=_CACHE_TTL,
                ),
            )
            _CACHE_REGISTRY[key] = cache.name
            return cache.name
        except Exception:
            # Too small to cache, no quota, or unsupported — never cache again.
            _CACHE_DISABLED.add(key)
            return None


def _evict_cache(model: str, system_instruction: str) -> None:
    key = _cache_key(model, system_instruction)
    with _CACHE_LOCK:
        _CACHE_REGISTRY.pop(key, None)


class GeminiService:
    """
    Gemini through Vertex AI.

    Requires:
      LLM_PROVIDER=gemini
      GOOGLE_CLOUD_PROJECT=...
      GOOGLE_CLOUD_LOCATION=asia-southeast1
      GEMINI_MODEL=gemini-2.5-flash
      GOOGLE_APPLICATION_CREDENTIALS=...
    """

    def __init__(self) -> None:
        s = settings
        if not s.google_cloud_project:
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT is missing but LLM_PROVIDER is gemini. "
                "Set GOOGLE_CLOUD_PROJECT in .env or switch LLM_PROVIDER=openai."
            )

        self.model = s.gemini_model
        self.default_temperature = s.llm_temperature

        self.client = genai.Client(
            # Honour the setting rather than hardcoding True:
            # GOOGLE_GENAI_USE_VERTEXAI is documented in .env.sample and passed by
            # docker-compose, so hardcoding made the flag silently inert.
            vertexai=s.google_genai_use_vertexai,
            project=s.google_cloud_project,
            location=s.google_cloud_location,
            http_options=HttpOptions(api_version="v1"),
        )

    def _build_config(
        self,
        *,
        temperature: float | None,
        system_instruction: str | None,
        max_output_tokens: int | None,
        response_schema: Type[BaseModel] | None,
        use_cache: bool,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "temperature": self.default_temperature if temperature is None else temperature,
        }
        if max_output_tokens is not None:
            config["max_output_tokens"] = max_output_tokens
        if response_schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = response_schema

        if system_instruction:
            cache_name = (
                _get_or_create_cache(self.client, self.model, system_instruction)
                if use_cache
                else None
            )
            if cache_name:
                config["cached_content"] = cache_name
            else:
                config["system_instruction"] = system_instruction

        return config

    async def generate_text(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._generate_text_sync,
            prompt,
            temperature=temperature,
            system_instruction=system_instruction,
            max_output_tokens=max_output_tokens,
        )

    def _generate_text_sync(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
        response_schema: Type[BaseModel] | None = None,
    ) -> str:
        started_ms = _now_ms()

        with mlflow_span(
            "llm.gemini_generate_content",
            inputs={"prompt": prompt},
            attributes={
                "model": self.model,
                "provider": "vertex_gemini",
                "temperature": self.default_temperature if temperature is None else temperature,
                "span_category": "llm",
            },
        ) as span:
            response = self._generate_content_with_cache_fallback(
                prompt,
                temperature=temperature,
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
                response_schema=response_schema,
            )

            latency_ms = max(0, _now_ms() - started_ms)
            usage = extract_gemini_usage(response)
            text = getattr(response, "text", "") or ""

            tracker = get_usage_tracker()
            if tracker is not None:
                tracker.add_llm_call(
                    name="gemini_generate_content",
                    latency_ms=latency_ms,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    total_tokens=usage["total_tokens"],
                    model=self.model,
                )

            if span is not None:
                span.set_outputs(
                    safe_dict(
                        {
                            "text_preview": text,
                            "latency_ms": latency_ms,
                            **usage,
                        },
                        limit=1000,
                    )
                )

            return text

    def _generate_content_with_cache_fallback(
        self,
        prompt: str,
        *,
        temperature: float | None,
        system_instruction: str | None,
        max_output_tokens: int | None,
        response_schema: Type[BaseModel] | None,
    ) -> Any:
        config = self._build_config(
            temperature=temperature,
            system_instruction=system_instruction,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema,
            use_cache=True,
        )

        used_cache = "cached_content" in config

        try:
            return self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
        except Exception:
            if not used_cache or not system_instruction:
                raise
            # A stale/expired cache reference is the likely cause. Evict it and
            # retry once with the system instruction sent inline.
            _evict_cache(self.model, system_instruction)
            config = self._build_config(
                temperature=temperature,
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
                response_schema=response_schema,
                use_cache=False,
            )
            return self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )

    async def stream_text(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        system_instruction: str | None = None,
    ) -> AsyncIterator[str]:
        # Streaming sends the static block inline (no explicit cache lifecycle on
        # the streaming path); implicit prefix caching still applies.
        config = self._build_config(
            temperature=temperature,
            system_instruction=system_instruction,
            max_output_tokens=None,
            response_schema=None,
            use_cache=False,
        )

        def iterator_factory():
            return self.client.models.generate_content_stream(
                model=self.model,
                contents=prompt,
                config=config,
            )

        def extract_text(chunk: Any) -> str:
            return getattr(chunk, "text", "") or ""

        def extract_usage(chunk: Any) -> dict[str, int]:
            return extract_gemini_usage(chunk)

        async for text in _stream_sync_iterator(
            iterator_factory=iterator_factory,
            extract_text=extract_text,
            extract_usage=extract_usage,
            span_name="gemini_generate_content_stream",
            model=self.model,
        ):
            yield text

    async def generate_json(
        self,
        prompt: str,
        schema: Type[T],
        *,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> T:
        # Preferred path: native structured output via response_schema.
        try:
            raw = await asyncio.to_thread(
                self._generate_text_sync,
                prompt,
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
                response_schema=schema,
            )
            return schema.model_validate(_extract_json(raw))
        except Exception:
            # Safety net: if Vertex rejects this response_schema, or the structured
            # output does not validate, fall back to the prompt-suffix approach.
            # This never regresses below the prior (no-schema) behavior.
            raw = await asyncio.to_thread(
                self._generate_text_sync,
                prompt + _JSON_INSTRUCTION,
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
            )
            return schema.model_validate(_extract_json(raw))


class OpenAIService:
    """
    OpenAI direct API.

    Requires:
      LLM_PROVIDER=openai
      OPENAI_API_KEY=...
      OPENAI_MODEL=gpt-4o-mini
    """

    def __init__(self) -> None:
        s = settings
        if not s.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is missing but LLM_PROVIDER is openai. "
                "Set OPENAI_API_KEY in .env or switch LLM_PROVIDER=gemini."
            )

        self.model = s.openai_model
        self.default_temperature = s.llm_temperature

        self.client = OpenAI(
            api_key=s.openai_api_key,
            timeout=s.llm_timeout_seconds,
            max_retries=s.llm_max_retries,
        )

    def _build_messages(self, prompt: str, system_instruction: str | None) -> list[dict[str, str]]:
        # A stable leading system message lets OpenAI's automatic prompt caching
        # reuse the static rule prefix across requests.
        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})
        return messages

    async def generate_text(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._generate_text_sync,
            prompt,
            temperature=temperature,
            system_instruction=system_instruction,
            max_output_tokens=max_output_tokens,
        )

    def _generate_text_sync(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        started_ms = _now_ms()

        with mlflow_span(
            "llm.openai_chat_completion",
            inputs={"prompt": prompt},
            attributes={
                "model": self.model,
                "provider": "openai",
                "temperature": self.default_temperature if temperature is None else temperature,
                "span_category": "llm",
            },
        ) as span:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": self._build_messages(prompt, system_instruction),
                "temperature": self.default_temperature if temperature is None else temperature,
            }
            if max_output_tokens is not None:
                kwargs["max_tokens"] = max_output_tokens
            if response_format is not None:
                kwargs["response_format"] = response_format

            response = self.client.chat.completions.create(**kwargs)

            latency_ms = max(0, _now_ms() - started_ms)
            usage = _extract_openai_usage(response)

            tracker = get_usage_tracker()
            if tracker is not None:
                tracker.add_llm_call(
                    name="openai_chat_completion",
                    latency_ms=latency_ms,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    total_tokens=usage["total_tokens"],
                    model=self.model,
                )

            if not response.choices:
                content = ""
            else:
                content = response.choices[0].message.content or ""

            if span is not None:
                span.set_outputs(
                    safe_dict(
                        {
                            "text_preview": content,
                            "latency_ms": latency_ms,
                            **usage,
                        },
                        limit=1000,
                    )
                )

            return content

    async def stream_text(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        system_instruction: str | None = None,
    ) -> AsyncIterator[str]:
        messages = self._build_messages(prompt, system_instruction)

        def iterator_factory():
            return self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.default_temperature if temperature is None else temperature,
                stream=True,
                stream_options={"include_usage": True},
            )

        def extract_text(chunk: Any) -> str:
            if not getattr(chunk, "choices", None):
                return ""

            delta = chunk.choices[0].delta
            return getattr(delta, "content", None) or ""

        def extract_usage(chunk: Any) -> dict[str, int]:
            return _extract_openai_usage(chunk)

        async for text in _stream_sync_iterator(
            iterator_factory=iterator_factory,
            extract_text=extract_text,
            extract_usage=extract_usage,
            span_name="openai_chat_completion_stream",
            model=self.model,
        ):
            yield text

    async def generate_json(
        self,
        prompt: str,
        schema: Type[T],
        *,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> T:
        raw = await asyncio.to_thread(
            self._generate_text_sync,
            prompt + _JSON_INSTRUCTION,
            system_instruction=system_instruction,
            max_output_tokens=max_output_tokens,
            response_format={"type": "json_object"},
        )
        return schema.model_validate(_extract_json(raw))


def _extract_openai_usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)

    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or input_tokens + output_tokens

    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens),
    }


def _salvage_truncated_json(text: str) -> dict | None:
    """Recover complete top-level "key": value pairs from a truncated JSON object.

    When the model's output is cut off inglegate-value (e.g. the response token limit is
    reached while writing a long `reason`), the object never closes and normal
    parsing fails. This extracts every COMPLETE scalar pair, skipping the
    unterminated trailing one, so callers with flat schemas can still validate
    (e.g. recover `intent`/`route` even if `reason` was truncated).
    """
    obj: dict[str, Any] = {}
    pair_re = re.compile(
        r'"([A-Za-z0-9_]+)"\s*:\s*'
        r'(?:"((?:[^"\\]|\\.)*)"|(true|false|null)|(-?\d+(?:\.\d+)?))'
    )
    for m in pair_re.finditer(text):
        key = m.group(1)
        if m.group(2) is not None:
            obj[key] = m.group(2)
        elif m.group(3) is not None:
            obj[key] = {"true": True, "false": False, "null": None}[m.group(3)]
        elif m.group(4) is not None:
            raw = m.group(4)
            obj[key] = float(raw) if ("." in raw) else int(raw)
    return obj or None


def _extract_json(text: str) -> Any:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass

    # Last resort: the object was likely truncated (e.g. output token limit hit
    # inglegate-value). Salvage the complete scalar pairs so flat schemas still validate.
    salvaged = _salvage_truncated_json(cleaned)
    if salvaged:
        return salvaged

    raise ValueError(f"Model did not return valid JSON: {cleaned[:500]}")


@lru_cache
def get_llm() -> LLMService:
    s = settings
    provider = s.llm_provider.lower().strip()

    if provider in {"gemini", "vertex"}:
        return GeminiService()

    if provider == "openai":
        return OpenAIService()

    raise ValueError(
        f"Unsupported LLM_PROVIDER={s.llm_provider!r}. "
        "Use LLM_PROVIDER=gemini or LLM_PROVIDER=openai."
    )
