"""地图工作流的版本化 schema、阶段映射和合法转换合同。"""

from __future__ import annotations

from typing import Final

MAP_WORKER_RESULT_SCHEMA: Final = "map_worker_result_v1"

MAP_RUNTIME_STAGE_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "read": frozenset({"read", "plan", "write"}),
    "plan": frozenset({"read", "plan", "write"}),
    "write": frozenset({"read", "plan", "write", "validate"}),
    "validate": frozenset(
        {"read", "plan", "write", "validate", "review", "diagnostic"}
    ),
    "review": frozenset({"read", "plan", "review", "diagnostic"}),
    "diagnostic": frozenset(
        {"read", "plan", "write", "validate", "review", "diagnostic"}
    ),
}

MAP_WORKER_TO_RUNTIME_STAGE: Final[dict[str, str]] = {
    "reader": "read",
    "planner": "plan",
    "writer": "write",
    "validator": "validate",
    "repairer": "write",
    "reviewer": "review",
}

MAP_WORKER_NEXT_STAGES: Final[dict[str, frozenset[str]]] = {
    "reader": frozenset({"reader", "planner", "replan"}),
    "planner": frozenset({"reader", "planner", "writer", "replan"}),
    "writer": frozenset({"planner", "validator", "reviewer", "replan"}),
    "validator": frozenset({"planner", "validator", "reviewer", "replan"}),
    "repairer": frozenset({"planner", "validator", "reviewer", "replan"}),
    "reviewer": frozenset(
        {"planner", "reviewer", "complete", "completed", "replan"}
    ),
}

MAP_WORKER_STAGES: Final = frozenset(MAP_WORKER_NEXT_STAGES)

