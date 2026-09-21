# app/service/mlflow_observability.py
from __future__ import annotations

import hashlib
import os
import re
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

from app.core import settings
from app.core.logging import Logger

logger = Logger.get_logger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s()]{7,}\d)(?!\d)")


def _shorten(value: str, limit: int = 4000) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[: limit - 20].rstrip() + "... [truncated]"


def redact_text(value: Any, *, limit: int = 4000) -> Any:
    """Redact obvious personal identifiers before sending payloads to MLflow."""
    if not isinstance(value, str):
        return value
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    return _shorten(text, limit=limit)


def safe_user_hash(user_id: str) -> str:
    clean = (user_id or "anonymous").strip().lower()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]


def safe_dict(payload: dict[str, Any] | None, *, limit: int = 4000) -> dict[str, Any]:
    if not payload:
        return {}

    safe: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            safe[key] = redact_text(value, limit=limit)
        elif isinstance(value, dict):
            safe[key] = safe_dict(value, limit=limit)
        elif isinstance(value, list):
            safe[key] = [redact_text(item, limit=limit) if isinstance(item, str) else item for item in value[:50]]
        else:
            safe[key] = value
    return safe


@lru_cache(maxsize=1)
def configure_mlflow() -> bool:
    """Configure MLflow tracing once per process.

    The app must not fail startup just because MLflow is temporarily unavailable.
    When disabled or misconfigured, this returns False and tracing becomes a no-op.
    """
    if not settings.mlflow_tracking_enabled:
        logger.info("MLflow tracking disabled")
        return False

    try:
        import mlflow
    except Exception as exc:  # pragma: no cover - defensive import guard
        logger.warning("MLflow package is unavailable; tracing disabled: %s", exc)
        return False

    try:
        if settings.mlflow_tracking_uri:
            mlflow.set_tracking_uri(settings.mlflow_tracking_uri)

        if settings.mlflow_experiment_name:
            mlflow.set_experiment(settings.mlflow_experiment_name)

        if settings.mlflow_gemini_autolog_enabled:
            # Traces every google-genai call -- prompt, response, model, token
            # usage -- which is the part of a request no explicit span can see.
            # MLflow documents compatibility up to google-genai 2.8.0 and this
            # project runs ahead of that, so a version bump that breaks the patch
            # must degrade to "no LLM spans", never to a failed startup.
            try:
                mlflow.gemini.autolog()
                logger.info("MLflow Gemini autolog enabled")
            except Exception as exc:
                logger.warning(
                    "MLflow Gemini autolog could not be enabled; LLM calls will "
                    "not be traced (explicit spans are unaffected): %s",
                    exc,
                )

        logger.info(
            "MLflow tracking configured: uri=%s experiment=%s otlp=%s",
            settings.mlflow_tracking_uri,
            settings.mlflow_experiment_name,
            bool(os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")),
        )
        return True

    except Exception as exc:
        logger.exception("MLflow tracking configuration failed; continuing without tracing: %s", exc)
        return False


@contextmanager
def mlflow_span(
    name: str,
    *,
    inputs: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any | None]:
    """Start an MLflow span when tracking is enabled; otherwise behave as no-op.

    Only MLflow's own failures are swallowed. An exception from the WRAPPED BODY
    propagates untouched, which the previous version did not do: it wrapped the
    `yield` in `try/except Exception`, so a body exception was thrown back in at
    the yield, caught here, and then the generator yielded a second time. Yielding
    again after a throw is illegal, so Python replaced the real error with
    `RuntimeError: generator didn't stop after throw()` -- at every nesting level.

    A one-line NameError in the answer agent therefore surfaced as a three-deep
    chain of RuntimeErrors, with the actual cause buried and the span logged as
    "failed and was skipped" as if MLflow were at fault. Observability must never
    edit the errors it observes.
    """
    if not configure_mlflow():
        yield None
        return

    try:
        import mlflow

        span_cm = mlflow.start_span(name=name)
    except Exception as exc:
        logger.warning("MLflow span %s could not be started and was skipped: %s", name, exc)
        yield None
        return

    with span_cm as span:
        # Attribute recording is best-effort; failing to annotate a span is not a
        # reason to fail the request.
        try:
            if inputs:
                span.set_inputs(safe_dict(inputs))
            if attributes:
                span.set_attributes(safe_dict(attributes, limit=1000))
        except Exception as exc:
            logger.warning("MLflow span %s could not record inputs: %s", name, exc)

        # Outside the try: a body exception propagates, and mlflow's own context
        # manager marks the span as errored on the way out -- which is what you
        # want in the trace.
        yield span


def update_current_trace(
    *,
    request_id: str,
    user_id: str,
    thread_id: str,
    query: str,
    route: str,
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    if not configure_mlflow():
        return

    try:
        import mlflow

        metadata = {
            "service.name": "customer-intelligence-copilot",
            "route": route,
            "thread_id": thread_id,
            "user_hash": safe_user_hash(user_id),
        }
        if extra_metadata:
            metadata.update(safe_dict(extra_metadata, limit=1000))

        mlflow.update_current_trace(
            client_request_id=request_id,
            user=safe_user_hash(user_id),
            session_id=thread_id,
            request_preview=redact_text(query, limit=300),
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning("Could not update MLflow current trace metadata: %s", exc)
