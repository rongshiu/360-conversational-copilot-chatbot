from __future__ import annotations

from app.service.entity_resolution.dictionary import (
    EntityDictionary,
    clear_dictionary_cache,
    get_dictionary,
    normalize,
)
from app.service.entity_resolution.models import (
    CATEGORY_CLASSES,
    EntityCandidate,
    EntityClass,
    EntitySpan,
    ResolutionPlan,
    ResolutionResult,
    ResolvedSlot,
)
from app.service.entity_resolution.service import EntityResolver, get_entity_resolver

__all__ = [
    "CATEGORY_CLASSES",
    "EntityCandidate",
    "EntityClass",
    "EntityDictionary",
    "EntityResolver",
    "EntitySpan",
    "ResolutionPlan",
    "ResolutionResult",
    "ResolvedSlot",
    "clear_dictionary_cache",
    "get_dictionary",
    "get_entity_resolver",
    "normalize",
]
