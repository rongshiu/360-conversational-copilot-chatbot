from __future__ import annotations
import math
from decimal import Decimal
from typing import Any
import json

from fastapi import HTTPException, status


def sanitize_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: sanitize_for_json(v) for k, v in value.items()}

    if isinstance(value, list):
        return [sanitize_for_json(v) for v in value]

    if isinstance(value, tuple):
        return [sanitize_for_json(v) for v in value]

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    return value


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(sanitize_for_json(data), ensure_ascii=False)}\n\n"


# An SSE comment. Conforming clients ignore it, so it keeps a slow connection
# alive through an intermediary's idle timeout without the client needing to know
# about a heartbeat event type.
SSE_KEEPALIVE = ": keepalive\n\n"


def require_user_id(x_user_id: str | None) -> str:
    user_id = (x_user_id or "").strip()
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing x-user-id header",
        )
    return user_id
