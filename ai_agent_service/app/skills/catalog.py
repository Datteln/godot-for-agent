"""Skill 发现、解析与加载。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.config import AppSettings
from app.security.paths import path_ok
from app.security.settings import SecuritySettings
from app.skills.types import SkillDefinition, SkillSource, SkillSummary

logger = logging.getLogger(__name__)


def _bundled_skills_dir() -> Path:
    return Path(__file__).parent / "bundled"


def _parse_scalar(value: str) -> str:
    return value.strip().strip("\"'")


def _parse_list(value: str) -> list[str]:
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in inner.split(",") if part.strip()]
    if "," in stripped:
        return [_parse_scalar(part) for part in stripped.split(",") if part.strip()]
    return [_parse_scalar(stripped)] if stripped else []


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析一个很小的 YAML frontmatter 子集，避免为 M0/M1 引入新依赖。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    meta: dict[str, Any] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if key in {"allowed-tools", "allowed_tools", "paths"}:
            meta[key] = _parse_list(value)
        else:
            meta[key] = _parse_scalar(value)

    body = "\n".join(lines[index:]).strip()
    return meta, body


def _source_dirs(settings: AppSettings) -> list[tuple[SkillSource, Path, bool]]:
    return [
        ("bundled", _bundled_skills_dir(), True),
        ("user", settings.user_skills_dir, True),
        ("project", settings.resolved_project_skills_dir(), settings.trusted_project),
    ]


class SkillCatalog:
    """Skill 注册表：按来源发现并按规范名加载正文。"""

    def __init__(
        self,
        settings: AppSettings,
        security: SecuritySettings,
        available_tools: set[str],
    ) -> None:
        self._settings = settings
        self._security = security
        self._available_tools = available_tools
        self._skills: dict[str, SkillDefinition] = {}
        self.refresh()

    def refresh(self) -> None:
        """重新扫描所有 Skill 目录。"""
        skills: dict[str, SkillDefinition] = {}
        for source, root, source_enabled in _source_dirs(self._settings):
            if not root.exists():
                logger.debug("Skill source skipped missing source=%s root=%s", source, root)
                continue
            for skill_file in sorted(root.glob("*/SKILL.md")):
                skill = self._load_file(source, skill_file, source_enabled)
                skills[skill.qualified_name] = skill
        self._skills = skills
        warnings = sum(len(skill.warnings) for skill in skills.values())
        logger.info("Skill catalog refreshed count=%d warnings=%d", len(skills), warnings)

    def summaries(self) -> list[SkillSummary]:
        """返回不含正文的 Skill 摘要。"""
        return [
            SkillSummary(
                qualified_name=skill.qualified_name,
                name=skill.name,
                source=skill.source,
                description=skill.description,
                when_to_use=skill.when_to_use,
                enabled=skill.enabled,
                effective_tools=skill.effective_tools,
                warnings=skill.warnings,
            )
            for skill in sorted(self._skills.values(), key=lambda s: s.qualified_name)
        ]

    def get(self, name: str) -> SkillDefinition | None:
        """按规范名或唯一短名查找 Skill。"""
        if name in self._skills:
            return self._skills[name]

        matches = [skill for skill in self._skills.values() if skill.name == name]
        if len(matches) == 1:
            return matches[0]
        return None

    def _load_file(
        self, source: SkillSource, skill_file: Path, source_enabled: bool
    ) -> SkillDefinition:
        text = skill_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)

        name = str(meta.get("name") or skill_file.parent.name)
        description = str(meta.get("description") or "")
        when_to_use = str(meta.get("when_to_use") or meta.get("when-to-use") or "")
        allowed_tools = list(meta.get("allowed-tools") or meta.get("allowed_tools") or [])
        paths = list(meta.get("paths") or [])
        warnings: list[str] = []

        if not description:
            warnings.append("缺少 description")
        if not when_to_use:
            warnings.append("缺少 when_to_use")

        enabled = source_enabled
        if source == "project" and not source_enabled:
            warnings.append("项目级 Skill 需要 trusted_project=true 后才启用")

        valid_paths: list[str] = []
        for path in paths:
            if path_ok(path, self._security, write=False):
                valid_paths.append(path)
            else:
                warnings.append(f"paths 中的路径不在允许范围内，已忽略：{path}")

        if not allowed_tools or allowed_tools == ["*"]:
            effective_tools = sorted(self._available_tools)
        else:
            effective_tools = sorted(set(allowed_tools) & self._available_tools)
            missing = sorted(set(allowed_tools) - self._available_tools)
            if missing:
                warnings.append(f"allowed-tools 中不可见的工具已忽略：{', '.join(missing)}")

        skill = SkillDefinition(
            qualified_name=f"{source}:{name}",
            name=name,
            source=source,
            description=description,
            when_to_use=when_to_use,
            body=body,
            file_path=skill_file,
            allowed_tools=allowed_tools,
            paths=valid_paths,
            effective_tools=effective_tools,
            enabled=enabled,
            warnings=warnings,
        )
        logger.debug(
            "Skill loaded qualified_name=%s enabled=%s tools=%d warnings=%d",
            skill.qualified_name,
            skill.enabled,
            len(skill.effective_tools),
            len(skill.warnings),
        )
        return skill
