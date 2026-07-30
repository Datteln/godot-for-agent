"""Server 工具执行上下文。

`ToolContext` 是 server 工具 handler 的唯一入参之一，携带本次调用所需的
安全边界与会话标识；handler 不直接访问全局状态，便于测试与权限校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.security.settings import SecuritySettings

if TYPE_CHECKING:
    from app.skills.catalog import SkillCatalog


@dataclass(frozen=True)
class ToolContext:
    """Server 工具 handler 执行所需的最小上下文。

    Attributes:
        security: 当前会话的安全边界配置，handler 内的路径访问必须经
            `app.security.paths.path_ok`/`all_paths_ok` 校验后才可读取。
        session_id: 当前会话 id，仅用于日志/审计关联，不作为信任凭据。
        session_epoch: 当前会话生命周期 epoch，用于拒绝 reset 前的旧 locator。
        skill_catalog: Skill 工具读取目录时使用的目录索引。
        effective_tools: 当前活跃 agent 帧实际可见的工具集合；server 工具可用它
            避免泄露当前入口/agent 不可见的工具元数据。
        agent_effective_tools: 阶段裁剪前的 agent 工具集合，用于区分阶段限制与
            agent 自身范围限制。
        workflow_stage: 当前地图工作流阶段；非地图帧为 None。
        agent_role: 当前 Agent 的稳定角色。
        worker_mode: 当前动态地图 Worker mode；永久 Agent 为 None。
        rag_index_path: 本地 RAG 索引文件路径；为空时 handler 使用工程内默认路径。
    """

    security: SecuritySettings
    session_id: str
    session_epoch: str = ""
    skill_catalog: SkillCatalog | None = None
    effective_tools: frozenset[str] = frozenset()
    agent_effective_tools: frozenset[str] = frozenset()
    workflow_stage: str | None = None
    agent_role: str | None = None
    worker_mode: str | None = None
    rag_index_path: Path | None = None
