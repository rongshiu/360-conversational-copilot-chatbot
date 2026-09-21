# app/graph/nodes/base.py
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class CopilotNodeDependencies:
    def __init__(
        self,
        db: AsyncSession,
        user_id: str,
        settings: Any,
        glossary_service: Any,
        entity_resolver: Any,
        principal: Any = None,
    ) -> None:
        self.db = db
        self.user_id = user_id
        self.principal = principal
        self.settings = settings
        self.glossary_service = glossary_service
        self.entity_resolver = entity_resolver
