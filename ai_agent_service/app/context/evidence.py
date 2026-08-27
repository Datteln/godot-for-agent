"""会话作用域的混合 Markdown 证据存储（任务 8.1/8.2/8.6）。

策略（设计决策 5）：

- **可复现来源**（项目文件、RAG、检索）：驻留状态只保留事实卡、定位符、
  源指纹与新鲜度；细节需要时从当前来源重新读取；
- **易失来源**（编辑器/运行时/诊断/命令/地图观察）：规范化 Markdown 写入
  会话作用域 sidecar 文件，驻留状态只保留索引、摘要、定位符、新鲜度、
  内容哈希与 sidecar 引用；
- sidecar 按内容哈希去重，会话重置/删除时一并清理；绝不保存原始结果
  JSON，也不跨会话存活。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.context.models import (
    REPRODUCIBLE_SOURCE_KINDS,
    ContextMemoryState,
    EvidenceRecord,
    ToolMemoryRecord,
)

logger = logging.getLogger(__name__)

_SIDE_DIR_NAME = "evidence"
"""sidecar 根目录名（会话存储目录的同级目录）。"""


def _utc_now() -> str:
    """返回当前 UTC ISO-8601 时间。"""
    return datetime.now(timezone.utc).isoformat()


def normalize_markdown(markdown: str) -> str:
    """内容哈希用的规范化形态：去首尾空白与每行行尾空白。"""
    return "\n".join(line.rstrip() for line in str(markdown).strip().splitlines())


def content_hash(markdown: str) -> str:
    """计算规范化 Markdown 的 SHA-256（sidecar 去重身份）。"""
    return hashlib.sha256(normalize_markdown(markdown).encode("utf-8")).hexdigest()


def _safe_dirname(session_id: str) -> str:
    """会话 id → 安全的目录名（SHA-256 十六进制）。"""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class EvidenceSidecarStore:
    """会话作用域的 Markdown sidecar 文件存储（按内容哈希去重）。

    Attributes:
        root: sidecar 根目录（`<存储目录>/evidence`）。
    """

    def __init__(self, storage_dir: Path | str) -> None:
        """以会话存储目录为基准构造 sidecar 根目录。

        Args:
            storage_dir: 会话 JSON 的存储目录（例如 `.ai_agent_service/sessions`）。
        """
        self.root = Path(storage_dir).parent / _SIDE_DIR_NAME

    def session_dir(self, session_id: str) -> Path:
        """返回某会话的 sidecar 目录（不创建）。"""
        return self.root / _safe_dirname(session_id)

    def write(self, session_id: str, markdown: str) -> tuple[str, bool]:
        """写入（或复用）一段规范化 Markdown 证据正文。

        Args:
            session_id: 会话 id。
            markdown: 规范化后的 Markdown 正文。

        Returns:
            `(sidecar 文件名, 是否复用了已存在的同哈希文件)`。
        """
        digest = content_hash(markdown)
        directory = self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.md"
        if path.exists():
            return path.name, True
        path.write_text(normalize_markdown(markdown), encoding="utf-8")
        logger.debug(
            "Evidence sidecar written session=%s file=%s bytes=%d",
            session_id,
            path.name,
            path.stat().st_size,
        )
        return path.name, False

    def read(self, session_id: str, sidecar_ref: str) -> str | None:
        """按引用读取 sidecar 正文；不存在时返回 None。"""
        path = self.session_dir(session_id) / sidecar_ref
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def delete_session(self, session_id: str) -> int:
        """删除会话的全部 sidecar（重置/删除会话时调用）。

        Returns:
            被删除的文件数量。
        """
        directory = self.session_dir(session_id)
        if not directory.exists():
            return 0
        removed = 0
        for item in list(directory.iterdir()):
            if item.is_file():
                item.unlink()
                removed += 1
        try:
            directory.rmdir()
        except OSError:
            pass
        if removed:
            logger.info("Evidence sidecars removed session=%s files=%d", session_id, removed)
        return removed


def record_evidence(
    state: ContextMemoryState,
    *,
    source_kind: str,
    tool_name: str,
    locator: str,
    target: str,
    facts: str,
    body_markdown: str = "",
    fingerprint: str = "",
    call_id: str = "",
    sidecars: EvidenceSidecarStore | None = None,
    session_id: str = "",
) -> EvidenceRecord:
    """登记一条证据索引（任务 8.1/8.2）。

    可复现来源只留索引+定位符+指纹；易失来源把规范化正文写入会话
    sidecar 并保留引用。相同内容哈希的记录复用既有索引（去重）。

    Args:
        state: 帧级记忆状态。
        source_kind: 来源类别。
        tool_name: 产生证据的工具名。
        locator: 获取细节的确切后续方式。
        target: 规范化目标。
        facts: 有界 Markdown 事实卡。
        body_markdown: 易失来源的完整规范化正文；缺省时使用事实卡。
        fingerprint: 可复现来源的源指纹。
        call_id: 关联的 tool_call id。
        sidecars: sidecar 存储（易失来源必需）。
        session_id: 会话 id（易失来源必需）。

    Returns:
        登记（或复用）的证据索引记录。
    """
    body = body_markdown.strip() or facts.strip()
    digest = content_hash(body)
    for existing in state.evidence_index.values():
        if (
            existing.content_hash == digest
            and existing.source_kind == source_kind
            and existing.target == target
            and existing.locator == locator
        ):
            existing.freshness = "observed"
            existing.updated_at = _utc_now()
            if call_id and call_id not in existing.call_ids:
                existing.call_ids.append(call_id)
            return existing

    sidecar_ref: str | None = None
    if source_kind not in REPRODUCIBLE_SOURCE_KINDS and sidecars is not None and session_id:
        sidecar_ref, _reused = sidecars.write(session_id, body)

    evidence_id = state.new_evidence_id()
    record = EvidenceRecord(
        evidence_id=evidence_id,
        source_kind=source_kind,
        tool_name=tool_name,
        locator=locator,
        target=target,
        facts=facts.strip(),
        fingerprint=fingerprint,
        content_hash=digest,
        freshness="observed",
        sidecar_ref=sidecar_ref,
        turn_id=state.current_turn_id,
        created_at=_utc_now(),
        updated_at=_utc_now(),
        call_ids=[call_id] if call_id else [],
    )
    state.evidence_index[evidence_id] = record
    state.revision += 1
    return record


def locator_for(
    tool_name: str, input_args: dict[str, Any], payload: Any
) -> str:
    """按来源语义推导后续精确检索的定位符（任务 8.4）。

    Args:
        tool_name: 工具名。
        input_args: 工具入参。
        payload: 解析后的结果载荷。

    Returns:
        人类/模型可读的后续检索方式描述。
    """
    body = payload if isinstance(payload, dict) else {}

    def _arg(*keys: str) -> str:
        for key in keys:
            value = input_args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    if tool_name in {"read_file", "read_resource"}:
        path = _arg("path", "target_path") or str(body.get("path", ""))
        offset = body.get("offset", input_args.get("offset", 1))
        return (
            f"read_file(path={path!r}, offset={offset}, limit=...) 按行范围精读；"
            "代码可用 selector={kind:'symbol', value:'符号名'}，配置可用 "
            "selector={kind:'match'|'json_path', value:'键或路径'}"
        )
    if tool_name in {"grep_code", "search_codebase"}:
        pattern = _arg("pattern", "query")
        return f"grep_code(pattern={pattern!r}, include=...) 或用命中的 路径:行号 精读"
    if tool_name == "list_files":
        return "list_files(pattern=...) 换更精确的模式，或对命中文件 read_file"
    if tool_name in {"read_scene_tree", "validate_scene_state"}:
        return (
            "read_scene_tree(node_path=...) 复查目标节点；"
            "set_node_property(node_path=..., property=...) 指定单个属性"
        )
    if tool_name in {"describe_map_region", "describe_tilemap_selection"}:
        target = str(body.get("target", "") or _arg("target_path"))
        return (
            f"describe_map_region(target_path={target!r}, x/y/width/height 有界分块) "
            "继续观察相邻区域；变更前必须重新观察"
        )
    if tool_name == "read_class_docs":
        class_name = str(body.get("class_name", "") or _arg("class_name", "class"))
        return f"read_class_docs(class_name={class_name!r}, mode=members/constants, 仅列所需成员)"
    if tool_name in {"read_debugger_errors", "read_runtime_state", "read_profiler_snapshot"}:
        return (
            f"{tool_name} 重新获取最新运行时快照；应在工具参数中指定 error_id、"
            "时间窗、node_path 或 property（运行时证据不可复现，需重读）"
        )
    path = _arg("path", "target_path", "node_path", "scene_path")
    if path:
        return f"{tool_name}(target={path!r}) 或更精确的选择器重查"
    return f"{tool_name} 按任务所需的选择器重新查询"


def render_fact_card(record: ToolMemoryRecord, evidence: EvidenceRecord) -> str:
    """把工具记录压缩为事实卡：保留身份/目标/定位符，去除可重读的细节。"""
    lines = [
        f"### 工具结果：{record.tool_name}（事实卡）",
        f"- 目标：{record.target}",
        f"- 新鲜度：{record.freshness}" + ("（已验证）" if record.verified else ""),
        f"- 证据：{evidence.evidence_id}（{evidence.source_kind}）",
        f"- 定位符：{evidence.locator}",
    ]
    if evidence.fingerprint:
        lines.append(f"- 源指纹：{evidence.fingerprint}")
    if evidence.sidecar_ref:
        lines.append(f"- sidecar：{evidence.sidecar_ref}（会话作用域可恢复）")
    if evidence.facts.strip():
        lines.append("")
        lines.append(evidence.facts.strip())
    return "\n".join(lines)


def reduce_tool_record_detail(state: ContextMemoryState) -> int:
    """把有定位符可恢复的工具记录收缩为事实卡（预算门的第一步）。

    仅处理与证据索引关联的记录；事实卡保留身份、目标、定位符与摘要，
    细节通过定位符重新获取。

    Returns:
        被收缩的记录数。
    """
    evidence_by_call: dict[str, EvidenceRecord] = {}
    for evidence in state.evidence_index.values():
        for call_id in evidence.call_ids:
            evidence_by_call.setdefault(call_id, evidence)
    reduced = 0
    for record in (*state.current_turn_records, *state.tool_records):
        evidence = next(
            (evidence_by_call[call_id] for call_id in record.call_ids if call_id in evidence_by_call),
            None,
        )
        if evidence is None or not evidence.locator:
            continue
        card = render_fact_card(record, evidence)
        if len(card) < len(record.markdown):
            record.markdown = card
            reduced += 1
    if reduced:
        state.revision += 1
    return reduced

_REPRODUCIBLE_TOOLS: dict[str, str] = {
    "read_file": "project_file",
    "read_resource": "project_file",
    "read_image_metadata": "project_file",
    "grep_code": "search",
    "search_codebase": "search",
    "list_files": "search",
    "search_tools": "search",
    "read_class_docs": "class_docs",
}
"""可复现工具 → 来源类别。"""

_VOLATILE_TOOL_KINDS: dict[str, str] = {
    "read_scene_tree": "editor",
    "validate_scene_state": "editor",
    "read_debugger_errors": "diagnostic",
    "read_runtime_state": "runtime",
    "read_profiler_snapshot": "runtime",
    "run_system_command": "command",
    "execute_gd_script": "command",
    "run_tests": "command",
    "run_headless_self_test": "command",
    "git_status": "command",
    "git_diff": "command",
    "export_project": "command",
    "describe_map_region": "map",
    "describe_tilemap_selection": "map",
    "capture_viewport_screenshot": "editor",
}
"""易失工具 → 来源类别；未列出的前端场景/节点编辑观察归为 editor。"""

_FRONT_SCENE_PREFIXES = (
    "add_node",
    "set_node_property",
    "delete_node",
    "reparent_node",
    "rename_node",
    "instance_scene",
    "duplicate_node",
    "connect_signal",
    "disconnect_signal",
    "add_to_group",
    "remove_from_group",
    "list_node_groups",
    "list_node_signals",
    "list_node_methods",
    "list_groups",
    "get_current_scene_path",
    "save_scene",
    "list_open_scenes",
    "open_scene",
    "bake_navigation_mesh",
    "set_project_setting",
    "read_project_setting",
    "list_autoloads",
    "add_autoload",
    "remove_autoload",
    "list_input_actions",
    "add_input_action",
    "remove_input_action",
    "list_export_presets",
    "reload_map_targets",
    "rebuild_map_builder",
    "create_resource",
    "set_resource_property",
    "create_animation_track",
    "create_shader_material",
    "create_sprite_frames_from_sheet",
)


def classify_source_kind(tool_name: str) -> str | None:
    """推导工具结果的证据来源类别；不适合登记的返回 None。"""
    if tool_name in _REPRODUCIBLE_TOOLS:
        return _REPRODUCIBLE_TOOLS[tool_name]
    if tool_name in _VOLATILE_TOOL_KINDS:
        return _VOLATILE_TOOL_KINDS[tool_name]
    if tool_name in _FRONT_SCENE_PREFIXES:
        return "editor"
    if tool_name in {"propose_script_edit", "apply_text_edit", "propose_tests", "propose_content_file"}:
        return "editor"
    return None


def register_tool_evidence(
    state: ContextMemoryState,
    *,
    tool_name: str,
    input_args: dict[str, Any],
    payload: Any,
    markdown: str,
    call_id: str,
    sidecars: EvidenceSidecarStore | None = None,
    session_id: str = "",
) -> EvidenceRecord | None:
    """为一条已渲染的工具结果登记证据索引（任务 8.1/8.2）。

    可复现来源只留事实卡+定位符+指纹；易失来源把 Markdown 正文写入会话
    sidecar。登记成功后把定位符脚注追加到对应工具记忆记录，使长期记忆
    也携带可检索身份。

    Returns:
        登记的证据记录；不适合登记的工具返回 None。
    """
    source_kind = classify_source_kind(tool_name)
    if source_kind is None:
        return None
    from app.context.tool_markdown import derive_identity

    _, target = derive_identity(tool_name, input_args, payload)
    locator = locator_for(tool_name, input_args, payload)
    facts_lines: list[str] = []
    for line in markdown.split("\n"):
        if line.startswith("###") or line.startswith("- ") or line.startswith("> "):
            facts_lines.append(line)
        if len(facts_lines) >= 12:
            break
    facts = "\n".join(facts_lines) or markdown[:240]
    record = record_evidence(
        state,
        source_kind=source_kind,
        tool_name=tool_name,
        locator=locator,
        target=target,
        facts=facts,
        body_markdown=markdown,
        fingerprint=content_hash(markdown) if source_kind in REPRODUCIBLE_SOURCE_KINDS else "",
        call_id=call_id,
        sidecars=sidecars,
        session_id=session_id,
    )
    memory_record = state.record_by_call_id(call_id)
    if memory_record is not None and record.locator:
        footer = f"\n- 证据：{record.evidence_id}；定位符：{record.locator}"
        if record.sidecar_ref:
            footer += f"；sidecar：{record.sidecar_ref}"
        if footer not in memory_record.markdown:
            memory_record.markdown += footer
    return record

