"""帧级 Markdown 会话/工具记忆数据模型（optimize-llm-conversation-context 任务 1.1）。

模型上下文与权威可见展示稿分离：每个 agent 帧持有自己的
`ContextMemoryState`，其中：

- `tool_records` / `current_turn_records` 保存 **Markdown** 形态的工具结果
  记录（`ToolMemoryRecord`），带稳定身份、来源、新鲜度、校验状态与
  范围/续读字段；
- 会话级长期事实（目标/约束/决定/已验证事实/已完成与待办工作/助手承诺）
  以有界 Markdown 行保存；
- 历史编辑器上下文归并为按身份可替换的 `EditorFact`。

本模块不引入任何按工具类别的 Markdown 长度上限；体积约束只来自
"整体模型上下文预算"（见 `app/config.py::context_budget_tokens`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Freshness = Literal["observed", "verified", "superseded"]
"""工具记录的新鲜度：观察所得 / 已通过编辑后校验 / 已被同身份新记录取代。"""

ToolOrigin = Literal["server", "front", "delegate", "system"]
"""工具记录的执行侧来源。"""

EvidenceSourceKind = Literal[
    "project_file",
    "rag",
    "search",
    "editor",
    "runtime",
    "diagnostic",
    "command",
    "map",
    "class_docs",
]
"""证据来源类别：可复现来源（项目文件/RAG/检索）与易失来源（编辑器/运行时/诊断/命令/地图）。"""

REPRODUCIBLE_SOURCE_KINDS: frozenset[str] = frozenset({"project_file", "rag", "search"})
"""可复现来源：会话内只保留事实+定位符+指纹，细节随时从当前来源重新读取。"""

CONVERSATION_MEMORY_BLOCK = "[conversation_memory]"
"""注入 system 层的命名记忆块前缀；缓存观测与测试据此识别该层。"""


@dataclass
class ToolMemoryRecord:
    """一条 Markdown 形态的工具结果记忆记录。

    记录是"按身份可替换"的：同一 `identity_key`（工具 + 目标）的新记录
    在整体预算压缩时取代旧记录；`range_*`/`has_more`/`continuation_hint`
    承载硬上下文窗口放不下时的范围/续读身份。

    Attributes:
        record_id: 帧内唯一身份，例如 `"tm3"`。
        tool_name: 产生该结果的工具名。
        identity_key: 合并/去重键（工具名 + 规范化目标）。
        target: 规范化目标描述（文件路径、场景/节点路径、命令、查询等）。
        markdown: 渲染后的 Markdown 结果正文（不含原始结果 JSON）。
        freshness: 新鲜度状态。
        verified: 是否已通过编辑后校验（与 `freshness="verified"` 同步）。
        terminal: 是否为取消/拒绝/超时/重置等终结性结果。
        origin: 执行侧来源（server/front/delegate/system）。
        source: 结果来源补充说明（如 `status=applied`、错误码）。
        turn_id: 产生该记录的可见用户轮次 id。
        created_at: 首次创建时间（UTC ISO-8601）。
        updated_at: 最近一次更新时间（UTC ISO-8601）。
        range_start: 已保留范围的起点（字符偏移）；无范围裁剪时为 None。
        range_end: 已保留范围的终点（字符偏移）；无范围裁剪时为 None。
        has_more: 是否存在被范围裁剪掉的后续内容。
        continuation_hint: 获取被裁剪范围的有界后续读取提示。
        call_ids: 对应该记录来源的 OpenAI tool_call id 列表（幂等合并用）。
    """

    record_id: str
    tool_name: str
    identity_key: str
    target: str
    markdown: str
    freshness: Freshness = "observed"
    verified: bool = False
    terminal: bool = False
    origin: ToolOrigin = "front"
    source: str = ""
    turn_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    range_start: int | None = None
    range_end: int | None = None
    has_more: bool = False
    continuation_hint: str = ""
    call_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 原生字典，供会话持久化。"""
        return {
            "record_id": self.record_id,
            "tool_name": self.tool_name,
            "identity_key": self.identity_key,
            "target": self.target,
            "markdown": self.markdown,
            "freshness": self.freshness,
            "verified": self.verified,
            "terminal": self.terminal,
            "origin": self.origin,
            "source": self.source,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "range_start": self.range_start,
            "range_end": self.range_end,
            "has_more": self.has_more,
            "continuation_hint": self.continuation_hint,
            "call_ids": list(self.call_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolMemoryRecord:
        """从持久化字典恢复记录；对缺失字段给出安全默认值。"""
        freshness = data.get("freshness", "observed")
        if freshness not in ("observed", "verified", "superseded"):
            freshness = "observed"
        origin = data.get("origin", "front")
        if origin not in ("server", "front", "delegate", "system"):
            origin = "front"
        raw_call_ids = data.get("call_ids", [])
        return cls(
            record_id=str(data.get("record_id", "")),
            tool_name=str(data.get("tool_name", "")),
            identity_key=str(data.get("identity_key", "")),
            target=str(data.get("target", "")),
            markdown=str(data.get("markdown", "")),
            freshness=freshness,
            verified=bool(data.get("verified", False)),
            terminal=bool(data.get("terminal", False)),
            origin=origin,
            source=str(data.get("source", "")),
            turn_id=data.get("turn_id"),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            range_start=data.get("range_start"),
            range_end=data.get("range_end"),
            has_more=bool(data.get("has_more", False)),
            continuation_hint=str(data.get("continuation_hint", "")),
            call_ids=[str(item) for item in raw_call_ids] if isinstance(raw_call_ids, list) else [],
        )


@dataclass
class EditorFact:
    """归一化后的当前编辑器事实（任务 3.4）。

    历史用户消息里的 `[editor_context]` JSON 快照在离开受保护的当前请求
    后被替换为按身份可替换的有界事实；同一 `identity` 的新事实取代旧事实。

    Attributes:
        identity: 事实身份键，例如 `"editor:selection"`。
        kind: 事实类别（selection/scene_tree/debugger_errors/...）。
        summary: 有界的规范化描述（单行或少量行）。
        turn_id: 提供该事实的用户轮次 id。
        updated_at: 最近更新时间（UTC ISO-8601）。
    """

    identity: str
    kind: str
    summary: str
    turn_id: str | None = None
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 原生字典。"""
        return {
            "identity": self.identity,
            "kind": self.kind,
            "summary": self.summary,
            "turn_id": self.turn_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditorFact:
        """从持久化字典恢复编辑器事实。"""
        return cls(
            identity=str(data.get("identity", "")),
            kind=str(data.get("kind", "")),
            summary=str(data.get("summary", "")),
            turn_id=data.get("turn_id"),
            updated_at=str(data.get("updated_at", "")),
        )




@dataclass
class EvidenceRecord:
    """证据索引记录（任务 8.1）。

    驻留会话状态里只保存索引、摘要、定位符、新鲜度、内容哈希与可选的
    sidecar 引用——绝不默认保存完整证据正文：可复现来源凭定位符重读，
    易失来源的正文存放在会话作用域的 Markdown sidecar 文件中。

    Attributes:
        evidence_id: 帧内唯一证据身份，例如 "ev2"。
        source_kind: 来源类别（EvidenceSourceKind）。
        tool_name: 产生证据的工具名。
        locator: 获取细节的确切方式（符号/行范围、配置键、节点/属性、
            地图 layer/bounds、日志窗口、运行时字段等）。
        target: 规范化目标描述。
        facts: 有界 Markdown 事实卡（摘要，非正文）。
        fingerprint: 可复现来源的源指纹（供重读前校验新鲜度）。
        content_hash: 规范化 Markdown 的 SHA-256，用于 sidecar 去重。
        freshness: 新鲜度。
        sidecar_ref: 易失证据对应的 sidecar 文件名；可复现来源为 None。
        turn_id: 产生证据的用户轮次。
        created_at: 创建时间（UTC ISO-8601）。
        updated_at: 最近更新时间（UTC ISO-8601）。
        call_ids: 关联的 tool_call id 列表。
    """

    evidence_id: str
    source_kind: str
    tool_name: str
    locator: str
    target: str
    facts: str
    fingerprint: str = ""
    content_hash: str = ""
    freshness: Freshness = "observed"
    sidecar_ref: str | None = None
    turn_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    call_ids: list[str] = field(default_factory=list)

    @property
    def reproducible(self) -> bool:
        """是否为可复现来源（不需要 sidecar 正文）。"""
        return self.source_kind in REPRODUCIBLE_SOURCE_KINDS

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 原生字典，供会话持久化。"""
        return {
            "evidence_id": self.evidence_id,
            "source_kind": self.source_kind,
            "tool_name": self.tool_name,
            "locator": self.locator,
            "target": self.target,
            "facts": self.facts,
            "fingerprint": self.fingerprint,
            "content_hash": self.content_hash,
            "freshness": self.freshness,
            "sidecar_ref": self.sidecar_ref,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "call_ids": list(self.call_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceRecord":
        """从持久化字典恢复证据索引记录。"""
        freshness = data.get("freshness", "observed")
        if freshness not in ("observed", "verified", "superseded"):
            freshness = "observed"
        raw_call_ids = data.get("call_ids", [])
        return cls(
            evidence_id=str(data.get("evidence_id", "")),
            source_kind=str(data.get("source_kind", "project_file")),
            tool_name=str(data.get("tool_name", "")),
            locator=str(data.get("locator", "")),
            target=str(data.get("target", "")),
            facts=str(data.get("facts", "")),
            fingerprint=str(data.get("fingerprint", "")),
            content_hash=str(data.get("content_hash", "")),
            freshness=freshness,
            sidecar_ref=data.get("sidecar_ref"),
            turn_id=data.get("turn_id"),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            call_ids=[str(item) for item in raw_call_ids] if isinstance(raw_call_ids, list) else [],
        )


@dataclass
class ContextMemoryState:
    """单个 agent 帧持有的模型上下文记忆状态（任务 1.1/1.2）。

    与 `frame.messages`（OpenAI 协议历史）并列持久化：
    - 长期区（`tool_records` + 会话事实）跨用户轮次存活；
    - 当前轮区（`current_turn_records`）在轮次成功结束时机械合并入长期区，
      不触发额外摘要模型调用。

    Attributes:
        goals: 当前会话目标（有界行）。
        constraints: 约束条件。
        decisions: 已达成的决定。
        facts: 已验证/已确认的事实。
        completed_work: 已完成工作。
        pending_work: 待办工作。
        assistant_facts: 从带 tool_calls 的 assistant 消息保留的用户可见文本。
        editor_facts: 按身份归一化的当前编辑器事实。
        tool_records: 长期 Markdown 工具记忆。
        current_turn_records: 当前用户轮的 Markdown 工具记忆。
        current_turn_id: 当前用户轮 id。
        merged_call_ids: 已合并进当前轮记忆的 tool_call id（幂等保护）。
        record_counter: `record_id` 分配计数器。
        revision: 长期记忆内容版本号；任何持久内容变化递增，供缓存失效。
    """

    goals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    completed_work: list[str] = field(default_factory=list)
    pending_work: list[str] = field(default_factory=list)
    assistant_facts: list[str] = field(default_factory=list)
    editor_facts: dict[str, EditorFact] = field(default_factory=dict)
    tool_records: list[ToolMemoryRecord] = field(default_factory=list)
    current_turn_records: list[ToolMemoryRecord] = field(default_factory=list)
    evidence_index: dict[str, EvidenceRecord] = field(default_factory=dict)
    evidence_counter: int = 0
    current_turn_id: str | None = None
    merged_call_ids: set[str] = field(default_factory=set)
    record_counter: int = 0
    revision: int = 0

    def new_record_id(self) -> str:
        """分配下一个工具记忆记录身份。"""
        self.record_counter += 1
        return f"tm{self.record_counter}"

    def new_evidence_id(self) -> str:
        """分配下一个证据索引身份。"""
        self.evidence_counter += 1
        return f"ev{self.evidence_counter}"

    def is_empty(self) -> bool:
        """状态是否不含任何可注入的记忆内容。"""
        return (
            not self.goals
            and not self.constraints
            and not self.decisions
            and not self.facts
            and not self.completed_work
            and not self.pending_work
            and not self.assistant_facts
            and not self.editor_facts
            and not self.tool_records
            and not self.current_turn_records
            and not self.evidence_index
        )

    def add_current_record(self, record: ToolMemoryRecord) -> None:
        """把一条完成的工具结果记录并入当前轮记忆。

        常规轮次保留每一条完成记录；按身份/新鲜度的合并只能在整体上下文
        预算压缩边界执行，避免同一文件的多次范围读取或连续操作被提前丢失。

        Args:
            record: 已完成渲染的 Markdown 工具记忆记录。
        """
        self.current_turn_records.append(record)

    def merge_current_turn(self) -> int:
        """把当前轮记录机械合并入长期记忆并清空当前轮区。

        合并规则：保持记录顺序直接追加；按身份/新鲜度的去重只能在整体
        预算压缩时进行。此处不调用任何摘要模型。

        Returns:
            本次并入长期区的记录数。
        """
        merged = 0
        for record in self.current_turn_records:
            self.tool_records.append(record)
            merged += 1
        self.current_turn_records = []
        self.merged_call_ids = set()
        if merged:
            self.revision += 1
        return merged

    def record_by_call_id(self, call_id: str) -> ToolMemoryRecord | None:
        """按 tool_call id 查找已合并的记录（当前轮 + 长期区）。"""
        for record in (*self.current_turn_records, *self.tool_records):
            if call_id in record.call_ids:
                return record
        return None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 原生字典，供会话持久化（任务 1.2）。"""
        return {
            "goals": list(self.goals),
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "facts": list(self.facts),
            "completed_work": list(self.completed_work),
            "pending_work": list(self.pending_work),
            "assistant_facts": list(self.assistant_facts),
            "editor_facts": {key: fact.to_dict() for key, fact in self.editor_facts.items()},
            "tool_records": [record.to_dict() for record in self.tool_records],
            "current_turn_records": [record.to_dict() for record in self.current_turn_records],
            "evidence_index": {
                key: record.to_dict() for key, record in self.evidence_index.items()
            },
            "evidence_counter": self.evidence_counter,
            "current_turn_id": self.current_turn_id,
            "merged_call_ids": sorted(self.merged_call_ids),
            "record_counter": self.record_counter,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextMemoryState:
        """从持久化字典恢复记忆状态；对旧格式/缺失字段安全降级为空状态。"""
        if not isinstance(data, dict):
            return cls()

        def _str_list(key: str) -> list[str]:
            raw = data.get(key, [])
            return [str(item) for item in raw] if isinstance(raw, list) else []

        editor_facts: dict[str, EditorFact] = {}
        raw_editor = data.get("editor_facts", {})
        if isinstance(raw_editor, dict):
            for key, value in raw_editor.items():
                if isinstance(value, dict):
                    editor_facts[str(key)] = EditorFact.from_dict(value)

        def _records(key: str) -> list[ToolMemoryRecord]:
            raw = data.get(key, [])
            if not isinstance(raw, list):
                return []
            return [ToolMemoryRecord.from_dict(item) for item in raw if isinstance(item, dict)]

        evidence_index: dict[str, EvidenceRecord] = {}
        raw_evidence = data.get("evidence_index", {})
        if isinstance(raw_evidence, dict):
            for key, value in raw_evidence.items():
                if isinstance(value, dict):
                    evidence_index[str(key)] = EvidenceRecord.from_dict(value)

        merged_raw = data.get("merged_call_ids", [])
        return cls(
            goals=_str_list("goals"),
            constraints=_str_list("constraints"),
            decisions=_str_list("decisions"),
            facts=_str_list("facts"),
            completed_work=_str_list("completed_work"),
            pending_work=_str_list("pending_work"),
            assistant_facts=_str_list("assistant_facts"),
            editor_facts=editor_facts,
            tool_records=_records("tool_records"),
            current_turn_records=_records("current_turn_records"),
            evidence_index=evidence_index,
            evidence_counter=int(data.get("evidence_counter", 0) or 0),
            current_turn_id=data.get("current_turn_id"),
            merged_call_ids={str(item) for item in merged_raw} if isinstance(merged_raw, list) else set(),
            record_counter=int(data.get("record_counter", 0) or 0),
            revision=int(data.get("revision", 0) or 0),
        )
