"""Skill 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SkillSource = Literal["bundled", "user", "project", "plugin"]


@dataclass(frozen=True)
class SkillDefinition:
    """Claude Code 同构 Skill：`SKILL.md` frontmatter + body。"""

    qualified_name: str
    name: str
    source: SkillSource
    description: str
    when_to_use: str
    body: str
    file_path: Path
    metadata_schema_version: int = 1
    required_capabilities: list[str] = field(default_factory=list)
    compatible_roles: list[str] = field(default_factory=list)
    compatible_stages: list[str] = field(default_factory=list)
    compatible_modes: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    # 按工具 domain 过滤的标签列表（如 ["map", "resource"]），与 allowed_tools 互斥；
    # 用于在 binding_status 中判断 Skill 与当前 Agent 能力的交集
    capability_tags: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    effective_tools: list[str] = field(default_factory=list)
    enabled: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillSummary:
    """Doctor/API 暴露的 Skill 摘要，不包含正文。"""

    qualified_name: str
    name: str
    source: SkillSource
    description: str
    when_to_use: str
    enabled: bool
    effective_tools: list[str]
    required_capabilities: list[str]
    compatible_roles: list[str]
    compatible_stages: list[str]
    compatible_modes: list[str]
    warnings: list[str]
