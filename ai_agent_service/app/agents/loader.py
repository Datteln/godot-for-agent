"""AgentDefinition markdown loader."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from app.agents.types import EFFORT_LEVELS, AgentDefinition, EffortLevel

logger = logging.getLogger(__name__)


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


def _parse_dict(value: str) -> dict[str, str]:
    """解析形如 `{on_start: "提示文本", other: 值}` 的单行 flat 字符串映射。

    用于 `hooks` 等简单 key-value frontmatter 字段；不支持嵌套结构。
    """
    stripped = value.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        stripped = stripped[1:-1].strip()
    if not stripped:
        return {}
    result: dict[str, str] = {}
    for part in stripped.split(","):
        if ":" not in part:
            continue
        key, raw_value = part.split(":", 1)
        key = _parse_scalar(key)
        if key:
            result[key] = _parse_scalar(raw_value)
    return result


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1", "on"}


def _parse_effort(value: Any) -> EffortLevel:
    """把 frontmatter 的 `effort` 原始值校验为合法 `EffortLevel`。

    Args:
        value: `meta.get("effort")` 的原始结果，类型未知（通常为 `str`）。

    Returns:
        合法的 `EffortLevel`；值缺失或不在 `EFFORT_LEVELS` 中时回退为 `"standard"`。
    """
    if value in EFFORT_LEVELS:
        return cast(EffortLevel, value)
    return "standard"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()

    meta: dict[str, Any] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if key in {"tools", "disallowed_tools", "disallowed-tools", "skills"}:
            meta[key] = _parse_list(value)
        elif key in {"can_delegate", "can-delegate"}:
            meta[key] = _parse_bool(value)
        elif key == "hooks":
            meta[key] = _parse_dict(value)
        elif key == "max_turns":
            try:
                meta[key] = int(value)
            except ValueError:
                meta[key] = 12
        else:
            meta[key] = _parse_scalar(value)

    return meta, "\n".join(lines[index:]).strip()


def load_agent_file(path: Path) -> AgentDefinition:
    """从 markdown 文件加载一个 bundled AgentDefinition。"""
    logger.debug("Loading agent definition path=%s", path)
    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    name = str(meta.get("name") or path.stem)
    hooks = meta.get("hooks")
    definition = AgentDefinition(
        name=name,
        source="bundled",
        description=str(meta.get("description") or ""),
        prompt=body,
        tools=list(meta.get("tools") or ["*"]),
        disallowed_tools=list(meta.get("disallowed_tools") or meta.get("disallowed-tools") or []),
        skills=list(meta.get("skills") or []),
        model=str(meta.get("model") or "inherit"),
        effort=_parse_effort(meta.get("effort")),
        max_turns=int(meta.get("max_turns") or 12),
        can_delegate=bool(meta.get("can_delegate") or meta.get("can-delegate") or False),
        hooks=hooks if isinstance(hooks, dict) and hooks else None,
    )
    logger.info(
        "Agent definition loaded name=%s source=%s tools=%d skills=%d max_turns=%d "
        "can_delegate=%s prompt_chars=%d",
        definition.name,
        definition.source,
        len(definition.tools),
        len(definition.skills),
        definition.max_turns,
        definition.can_delegate,
        len(definition.prompt),
    )
    return definition
