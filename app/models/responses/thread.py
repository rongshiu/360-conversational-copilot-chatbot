from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class ThreadListResponse(BaseModel):
    threads: List[Dict[str, Any]] = Field(default_factory=list)


class ThreadMessagesResponse(BaseModel):
    messages: List[Dict[str, Any]] = Field(default_factory=list)


class MutationResponse(BaseModel):
    ok: bool
