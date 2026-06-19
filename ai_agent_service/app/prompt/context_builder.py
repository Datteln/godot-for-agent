"""四层 cache-aware 上下文结构。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ContextLayer(str, Enum):
    STABLE_PREFIX = "STABLE_PREFIX"
    STRUCTURE_CONTEXT = "STRUCTURE_CONTEXT"
    DYNAMIC_CONTEXT = "DYNAMIC_CONTEXT"
    QUERY = "QUERY"


@dataclass(frozen=True)
class ContextSection:
    layer: ContextLayer
    content: str
    cacheable: bool


@dataclass(frozen=True)
class CacheAwareContext:
    stable_prefix: str = ""
    structure_context: str = ""
    dynamic_context: str = ""
    query: str = ""

    def sections(self) -> list[ContextSection]:
        values = [
            ContextSection(ContextLayer.STABLE_PREFIX, self.stable_prefix, True),
            ContextSection(ContextLayer.STRUCTURE_CONTEXT, self.structure_context, True),
            ContextSection(ContextLayer.DYNAMIC_CONTEXT, self.dynamic_context, False),
            ContextSection(ContextLayer.QUERY, self.query, False),
        ]
        return [section for section in values if section.content.strip()]


class ContextBuilder:
    def build(self, *, stable_prefix: str, structure_context: str = "", dynamic_context: str = "", query: str = "") -> CacheAwareContext:
        context = CacheAwareContext(
            stable_prefix.strip(), structure_context.strip(), dynamic_context.strip(), query.strip()
        )
        logger.debug(
            "Cache-aware context built stable_chars=%d structure_chars=%d dynamic_chars=%d "
            "query_length=%d sections=%d cacheable_sections=%d",
            len(context.stable_prefix),
            len(context.structure_context),
            len(context.dynamic_context),
            len(context.query),
            len(context.sections()),
            sum(section.cacheable for section in context.sections()),
        )
        return context
