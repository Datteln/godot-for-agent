"""定义 Map worker 结构化结果的稳定常量合同。"""

from __future__ import annotations

from app.orchestrator.map_contracts import (
    MAP_WORKER_RESULT_SCHEMA,
    MAP_WORKER_STAGES,
    map_worker_required_fields,
)

MAP_WORKER_RESULT_FIELDS = map_worker_required_fields()
MAP_WORKER_STAGE_NAMES = MAP_WORKER_STAGES
MAP_OUTPUT_SCHEMA_V1 = MAP_WORKER_RESULT_SCHEMA
MAP_DELEGATE_LIST_LIMIT = 12
MAP_DELEGATE_TEXT_LIMIT = 1200
MAP_DELEGATE_DROP_KEYS = frozenset(
    {
        "cells",
        "full_cells",
        "raw_cells",
        "atlas_summary",
        "matches",
        "screenshot_base64",
        "image_base64",
        "data_url",
    }
)
