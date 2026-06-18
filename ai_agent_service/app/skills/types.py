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
    allowed_tools: list[str] = field(default_factory=list)
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
    warnings: list[str]
