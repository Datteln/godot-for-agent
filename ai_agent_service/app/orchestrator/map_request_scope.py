"""Request-scoped intent and lineage for map workflow completion gating."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal

MapRequestIntent = Literal["general", "map_read", "map_plan", "map_validate", "map_edit"]

_PLAN_ONLY_PHRASES = (
    "不要执行",
    "不要修改",
    "不修改",
    "只规划",
    "仅规划",
    "只给方案",
    "仅给方案",
    "只分析",
    "仅分析",
    "do not edit",
    "do not modify",
    "don't edit",
    "don't modify",
    "plan only",
    "without editing",
    "without modifying",
    "analysis only",
    "analyze only",
)
_CODE_OR_CONTROL_PHRASES = (
    "地图脚本",
    "地图代码",
    "map script",
    "map code",
    "map-agent",
    "map agent",
    "completion gate",
)
_CHINESE_MAP_CONTENT_TERMS = (
    "地图",
    "地形",
    "瓦片",
    "图块",
    "平台",
    "关卡",
    "路线",
    "道路",
    "障碍",
    "碰撞",
    "金币",
    "敌人",
    "终点",
    "出生点",
    "传送点",
    "装饰",
)
_CHINESE_EDIT_TERMS = (
    "创建",
    "新建",
    "生成",
    "修改",
    "编辑",
    "扩建",
    "扩展",
    "扩大",
    "删除",
    "移除",
    "放置",
    "添加",
    "绘制",
    "画",
    "铺",
    "填充",
    "移动",
    "替换",
    "修复",
    "清除",
    "重建",
    "调整",
)
_CHINESE_READ_TERMS = ("读取", "查看", "看看", "解释", "分析", "检查", "观察", "是什么")
_CHINESE_VALIDATE_TERMS = ("验证", "校验", "检测", "可达性", "连通性")
_CHINESE_PLAN_TERMS = ("规划", "方案", "设计", "怎么做", "如何做")
_EXPLICIT_CONTINUATION_PATTERNS = (
    re.compile(r"(继续|接着|恢复).{0,12}(地图|地形|瓦片|图块|平台|关卡).{0,12}(编辑|修改|生成|扩建|修复|任务)"),
    re.compile(r"(继续|接着|恢复).{0,12}(编辑|修改|生成|扩建|修复).{0,12}(地图|地形|瓦片|图块|平台|关卡)"),
    re.compile(r"(继续|接着|恢复).{0,12}(刚才|之前|上次).{0,12}(地图|关卡|编辑|修改)"),
    re.compile(
        r"\b(?:continue|resume)\b.{0,40}\b(?:map|level|tilemap|terrain)\b.{0,40}"
        r"\b(?:edit|change|task|work)\b",
        re.IGNORECASE,
    ),
)
_GENERIC_CONTINUATION_PATTERNS = (
    re.compile(
        r"^(?:请)?(?:继续|接着|恢复)(?:一下)?"
        r"(?:(?:刚才|之前|上次|当前|这个|该)(?:的)?)?"
        r"(?:任务|工作|操作|流程)?[。！!，, ]*$"
    ),
    re.compile(
        r"^(?:please\s+)?(?:continue|resume)"
        r"(?:\s+(?:the\s+)?(?:current|previous|last|this))?"
        r"(?:\s+(?:task|work|operation|workflow))?[.! ,]*$",
        re.IGNORECASE,
    ),
)
_CONTINUATION_NEGATIONS = (
    "不要继续",
    "别继续",
    "停止继续",
    "do not continue",
    "don't continue",
    "stop continuing",
)
_ENGLISH_MAP_CONTENT_RE = re.compile(
    r"\b(?:map|tilemap|tile\s+map|terrain|level|platform|route|path|obstacle|"
    r"collision|coin|enemy|goal|spawn|teleporter|decoration)\b",
    re.IGNORECASE,
)
_ENGLISH_EDIT_RE = re.compile(
    r"\b(?:create|generate|edit|modify|change|expand|extend|delete|remove|place|"
    r"paint|fill|move|replace|repair|fix|rebuild|adjust|build)\b",
    re.IGNORECASE,
)
_ENGLISH_READ_RE = re.compile(
    r"\b(?:read|show|explain|analy[sz]e|inspect|observe|describe)\b",
    re.IGNORECASE,
)
_ENGLISH_VALIDATE_RE = re.compile(
    r"\b(?:validate|verify|check|test|reachability|connectivity)\b",
    re.IGNORECASE,
)
_ENGLISH_PLAN_RE = re.compile(r"\b(?:plan|design|proposal|approach)\b", re.IGNORECASE)


@dataclass(frozen=True)
class MapRequestScope:
    """Persisted identity that binds one user request to an optional map task."""

    request_id: str = ""
    lineage_id: str = ""
    intent: MapRequestIntent = "general"
    map_task_id: str = ""
    origin_text_digest: str = ""
    explicit_continuation: bool = False
    completion_candidate: bool = False

    @property
    def activates_map_gate(self) -> bool:
        """Return whether this request is authorized to reach the map gate."""
        return (
            self.intent == "map_edit"
            and bool(self.lineage_id)
            and bool(self.map_task_id)
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-native representation for Session persistence."""
        return {
            "request_id": self.request_id,
            "lineage_id": self.lineage_id,
            "intent": self.intent,
            "map_task_id": self.map_task_id,
            "origin_text_digest": self.origin_text_digest,
            "explicit_continuation": self.explicit_continuation,
            "completion_candidate": self.completion_candidate,
        }

    @classmethod
    def from_dict(cls, value: Any) -> MapRequestScope:
        """Restore a scope, defaulting invalid or legacy values to non-edit."""
        if not isinstance(value, dict):
            return cls()
        raw_intent = value.get("intent")
        intent: MapRequestIntent = (
            raw_intent
            if raw_intent in {"general", "map_read", "map_plan", "map_validate", "map_edit"}
            else "general"
        )
        return cls(
            request_id=str(value.get("request_id", "")),
            lineage_id=str(value.get("lineage_id", "")),
            intent=intent,
            map_task_id=str(value.get("map_task_id", "")),
            origin_text_digest=str(value.get("origin_text_digest", "")),
            explicit_continuation=value.get("explicit_continuation") is True,
            completion_candidate=value.get("completion_candidate") is True,
        )


def is_continuation_intent(user_message: str) -> bool:
    """判断文本是否明确表达续作，但不据此授予任何地图编辑权限。

    Args:
        user_message: 当前用户消息原文。

    Returns:
        文本是否属于明确续作表达。
    """
    normalized = " ".join(user_message.strip().lower().split())
    if not normalized or any(phrase in normalized for phrase in _CONTINUATION_NEGATIONS):
        return False
    return any(
        pattern.search(normalized) is not None
        for pattern in (*_EXPLICIT_CONTINUATION_PATTERNS, *_GENERIC_CONTINUATION_PATTERNS)
    )


def classify_map_request(
    user_message: str,
    *,
    dedicated_resume_authorized: bool = False,
) -> tuple[MapRequestIntent, bool]:
    """Classify explicit map semantics without consulting historical map state.

    The classifier is intentionally conservative: uncertainty falls back to a
    non-edit intent so editor context, an old task id, or an ambiguous
    ``continue`` cannot authorize map mutation or completion gating.
    """
    normalized = " ".join(user_message.strip().lower().split())
    explicit_continuation = dedicated_resume_authorized or is_continuation_intent(normalized)
    if dedicated_resume_authorized:
        return "map_edit", True

    has_map_content = any(term in normalized for term in _CHINESE_MAP_CONTENT_TERMS)
    has_map_content = has_map_content or _ENGLISH_MAP_CONTENT_RE.search(normalized) is not None
    if not has_map_content:
        return "general", False

    plan_only = any(phrase in normalized for phrase in _PLAN_ONLY_PHRASES)
    has_specific_content = any(
        term in normalized
        for term in _CHINESE_MAP_CONTENT_TERMS
        if term != "地图"
    )
    code_or_control_only = (
        any(phrase in normalized for phrase in _CODE_OR_CONTROL_PHRASES)
        and not has_specific_content
    )
    has_edit = any(term in normalized for term in _CHINESE_EDIT_TERMS)
    has_edit = has_edit or _ENGLISH_EDIT_RE.search(normalized) is not None
    if has_edit and not plan_only and not code_or_control_only:
        return "map_edit", explicit_continuation

    has_validation = any(term in normalized for term in _CHINESE_VALIDATE_TERMS)
    has_validation = has_validation or _ENGLISH_VALIDATE_RE.search(normalized) is not None
    if has_validation:
        return "map_validate", False

    has_plan = any(term in normalized for term in _CHINESE_PLAN_TERMS)
    has_plan = has_plan or _ENGLISH_PLAN_RE.search(normalized) is not None
    if has_plan:
        return "map_plan", False

    has_read = any(term in normalized for term in _CHINESE_READ_TERMS)
    has_read = has_read or _ENGLISH_READ_RE.search(normalized) is not None
    return ("map_read", False) if has_read else ("general", False)


def new_request_scope(
    *,
    request_id: str | None,
    user_message: str,
    dedicated_resume_authorized: bool = False,
) -> MapRequestScope:
    """Create an isolated scope for a new user-authored request."""
    intent, explicit_continuation = classify_map_request(
        user_message,
        dedicated_resume_authorized=dedicated_resume_authorized,
    )
    digest = hashlib.sha256(user_message.encode("utf-8", errors="replace")).hexdigest()
    lineage_seed = request_id.strip() if isinstance(request_id, str) else ""
    lineage_id = (
        f"request:{lineage_seed}"
        if lineage_seed
        else f"request:{uuid.uuid4().hex}"
    )
    return MapRequestScope(
        request_id=lineage_seed,
        lineage_id=lineage_id,
        intent=intent,
        origin_text_digest=digest,
        explicit_continuation=explicit_continuation,
    )


def bind_map_task(scope: MapRequestScope, task_id: str) -> MapRequestScope:
    """Bind a classified map-edit request to one concrete map task."""
    if scope.intent != "map_edit" or not task_id:
        return scope
    return replace(scope, map_task_id=task_id, completion_candidate=False)


def mark_completion_candidate(
    scope: MapRequestScope,
    *,
    lineage_id: str,
    map_task_id: str,
) -> MapRequestScope:
    """Mark a successful map mutation as a completion candidate if lineage matches."""
    if (
        scope.activates_map_gate
        and scope.lineage_id == lineage_id
        and scope.map_task_id == map_task_id
    ):
        return replace(scope, completion_candidate=True)
    return scope
