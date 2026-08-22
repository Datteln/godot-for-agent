"""权威聊天展示稿的机器可读契约（快照、条目、补丁、修订版本）。

本模块是 `contract.md` 的代码化表达，同时约束 HTTP 历史快照、HTTP 命令确认
与 WebSocket `transcript_patch` 载荷三处消费方。所有字段均为 JSON 原生类型，
可直接进出持久化文件与线上载荷。

契约要点：
- 每个可见条目有稳定 `entry_id`、不可变 `ordinal`、类型化 `kind`/`state`、
  单调 `revision`（每次更新递增，创建为 1）。
- 快照携带 `upto_event_seq` 游标，与 WebSocket 订阅 `after_seq` 同一序号空间。
- 补丁携带目标条目的完整最新状态（累计式），凭 `event_id` 幂等、凭
  `revision` 拒绝过期更新。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

TRANSCRIPT_CONTRACT_VERSION = 1
"""当前展示稿契约版本号；不兼容变更必须递增。"""

TranscriptKind = Literal[
    "user",
    "assistant",
    "thought",
    "tool_activity",
    "approval",
    "plan",
    "progress",
    "verification",
    "error",
    "system",
    "log",
]
"""展示稿条目类型；渲染器仅凭该值与 typed payload 选择渲染方式。"""

VALID_ENTRY_STATES: dict[str, frozenset[str]] = {
    "user": frozenset({"complete"}),
    "assistant": frozenset({"streaming", "complete"}),
    "thought": frozenset({"thinking", "complete"}),
    "tool_activity": frozenset({"running", "resolved", "failed"}),
    "approval": frozenset({"pending", "approved", "rejected"}),
    "plan": frozenset({"complete"}),
    "progress": frozenset({"running", "complete"}),
    "verification": frozenset({"running", "passed", "failed"}),
    "error": frozenset({"complete"}),
    "system": frozenset({"complete"}),
    "log": frozenset({"complete"}),
}
"""各 `kind` 的合法 `state` 取值，供写入端在产生时校验。"""

LEGACY_ONLY_KINDS = frozenset({"system", "log"})
"""仅允许由旧会话一次性兼容转换产生的条目类型。"""

TERMINAL_ENTRY_STATES: dict[str, frozenset[str]] = {
    "user": frozenset({"complete"}),
    "assistant": frozenset({"complete"}),
    "thought": frozenset({"complete"}),
    "tool_activity": frozenset({"resolved", "failed"}),
    "approval": frozenset({"approved", "rejected"}),
    "plan": frozenset({"complete"}),
    "progress": frozenset({"complete"}),
    "verification": frozenset({"passed", "failed"}),
    "error": frozenset({"complete"}),
    "system": frozenset({"complete"}),
    "log": frozenset({"complete"}),
}
"""各 `kind` 的终态集合；到达终态后条目内容不应再变化。"""


class TranscriptEntryDTO(BaseModel):
    """展示稿中一个用户可见条目的完整线上形态。

    Attributes:
        entry_id: 会话内唯一且永不复用的稳定身份（形如 `e12`）。
        ordinal: 不可变展示顺序，等于条目创建顺序。
        kind: 条目类型，决定 payload schema 与渲染器选择。
        state: 条目当前状态，取值受 `VALID_ENTRY_STATES` 约束。
        revision: 单调修订号，创建为 1，每次更新递增。
        turn_id: 产生该条目的轮次 id，可空。
        tool_call_id: 关联的工具调用 id，工具/审批类条目必填。
        payload: 按 `kind` 区分 schema 的完整内容载荷。
    """

    entry_id: str
    ordinal: int
    kind: TranscriptKind
    state: str
    revision: int = 1
    turn_id: str | None = None
    tool_call_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class TranscriptPatchDTO(BaseModel):
    """`transcript_patch` 事件的载荷：一条幂等的条目更新。

    补丁携带目标条目的完整最新状态（累计式）；客户端凭事件包络的
    `event_id` 去重，凭 `entry.revision` 拒绝不高于已接受修订的更新。

    Attributes:
        entry: 目标条目的完整最新状态。
        stream_key: 供传输层对同一条目的高频补丁做限速合并的分段键，
            恒等于 `entry.entry_id`。
    """

    entry: TranscriptEntryDTO
    stream_key: str


class TranscriptSnapshotDTO(BaseModel):
    """历史接口返回的原子展示稿快照。

    Attributes:
        version: 契约版本号。
        session_id: 快照所属会话 id。
        upto_event_seq: 原子切点游标：所有 `seq <= upto_event_seq` 的可见
            展示稿状态均已反映在 `entries` 中；客户端以该值作为 WebSocket
            订阅的 `after_seq`。
        legacy: True 表示快照来自旧会话的一次性兼容转换。
        entries: 按 `ordinal` 升序排列的条目列表。
    """

    version: int = TRANSCRIPT_CONTRACT_VERSION
    session_id: str
    upto_event_seq: int
    legacy: bool = False
    entries: list[TranscriptEntryDTO] = Field(default_factory=list)


def entry_to_wire(entry: TranscriptEntryDTO) -> dict[str, Any]:
    """把条目模型序列化为线上/持久化用的纯 JSON 字典。

    Args:
        entry: 待序列化的条目。

    Returns:
        仅含 JSON 原生类型的字典，字段与 `TranscriptEntryDTO` 一致。
    """
    return entry.model_dump()


def entry_from_wire(data: dict[str, Any]) -> TranscriptEntryDTO | None:
    """把线上/持久化字典解析为条目模型；字段缺失或非法时返回 None。

    Args:
        data: `entry_to_wire` 产出的字典（或持久化文件中的等价结构）。

    Returns:
        解析成功的条目模型；结构不合法时返回 None，由调用方决定跳过策略。
    """
    try:
        return TranscriptEntryDTO.model_validate(data)
    except (ValueError, TypeError):
        return None
