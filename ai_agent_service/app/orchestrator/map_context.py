"""宏观计划上下文：owner/lineage 链接、修订号、修订查询与进度摘要。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, TYPE_CHECKING
from app.orchestrator.map_artifacts import MapArtifactStore
from app.orchestrator.map_planning_contexts import (
    MapPlanningContextBundle,
    MapPlanningContextEntry,
    MapPlanningContextError,
)
from app.orchestrator.map_planning_snapshots import planning_snapshot_scope
from app.orchestrator.map_workflow import (
    dispatch_map_workflow_event,
    make_map_workflow_event,
)
from .map_state import MapTaskState
if TYPE_CHECKING:
    from app.sessions.store import Session
def record_map_owner_link(
    state: MapTaskState,
    *,
    macro_step_id: str,
    owner_frame_id: str,
    domain_task_id: str,
    target: str,
    revision: int,
) -> None:
    """记录地图域工作流与宏观计划的类型化链接（owner/macro/域任务身份）。

    独立于 MacroPlanState 持久化；两者通过 (macro_step_id, domain_task_id,
    owner_frame_id) 稳定关联，跨重试/审批/恢复 resume 同一 owner。
    """
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "map_owner_linked",
            target,
            revision,
            {
                "macro_step_id": macro_step_id,
                "owner_frame_id": owner_frame_id,
                "domain_task_id": domain_task_id,
            },
        ),
    )


def record_map_child_lineage(
    state: MapTaskState,
    *,
    child_frame_id: str,
    child_stage: str,
    task_stage: str | None = None,
    expected_task_stage: str | None = None,
    target: str | None = None,
    revision: int | None = None,
    planning_context_bundle_id: str | None = None,
    planning_context_bundle: dict[str, Any] | None = None,
    execution_operations: list[dict[str, Any]] | None = None,
) -> None:
    """原子记录 specialist 子帧 lineage 与对应任务阶段转换。"""
    workflow_identity = (
        target.strip()
        if isinstance(target, str) and target.strip()
        else f"__workflow__:{state.task_id or state.task_lineage_id or 'map-task'}"
    )
    event_revision = (
        revision
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0
        else state.structure_revision
    )
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "map_child_started",
            workflow_identity,
            event_revision,
            {
                "child_frame_id": child_frame_id,
                "child_stage": child_stage,
                "task_stage": task_stage or state.stage,
                "expected_task_stage": expected_task_stage or state.stage,
                "task_id": state.task_id,
                "owner_frame_id": state.owner_frame_id,
                "planning_context_bundle_id": planning_context_bundle_id,
                "planning_context_bundle": deepcopy(planning_context_bundle),
                "execution_operations": deepcopy(execution_operations or []),
            },
        ),
    )


def record_map_owner_publication(
    state: MapTaskState,
    *,
    publication: dict[str, Any],
    target: str,
    revision: int,
) -> None:
    """记录 owner 发布的类型化结果（preview_ready/awaiting_confirmation 等）。"""
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "map_owner_published",
            target,
            revision,
            {"publication": dict(publication)},
        ),
    )


def record_map_approval_identity(
    state: MapTaskState,
    *,
    approval_identity: dict[str, Any],
    target: str,
    revision: int,
) -> None:
    """记录审批身份，供 stale 审批拒绝与 owner resume 复用。"""
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "map_approval_recorded",
            target,
            revision,
            {"approval_identity": dict(approval_identity)},
        ),
    )


def record_planning_context_refresh(
    state: MapTaskState,
    *,
    context_entry: MapPlanningContextEntry,
    target: str,
    revision: int,
) -> None:
    """记录单个规划上下文的独立刷新，保证不相关上下文不受影响。

    与 replace_map_state_field 的全量替换不同，本函数通过专用 reducer
    事件 upsert 指定 context_id 的条目并重新计算 planning_context_bundle，
    确保刷新一个 gameplay 或 background 条目时，注册表中所有其他已注册
    上下文保持不变。
    """
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "planning_context_refreshed",
            target,
            revision,
            {
                "context_id": context_entry.context_id,
                "context_entry": context_entry.to_dict(),
                "resulting_bundle": (
                    _build_resulting_bundle(state, context_entry)
                    if state.planning_contexts
                    else None
                ),
            },
        ),
    )


def _build_resulting_bundle(
    state: MapTaskState,
    refreshed_entry: MapPlanningContextEntry,
) -> dict[str, Any] | None:
    """用刷新后的条目替换同 context_id 的旧条目，重建集合。"""
    entries: list[MapPlanningContextEntry] = [refreshed_entry]
    for entry_dict in state.planning_contexts.values():
        if not isinstance(entry_dict, dict):
            continue
        entry = MapPlanningContextEntry.from_dict(entry_dict)
        if entry.context_id != refreshed_entry.context_id:
            entries.append(entry)
    try:
        return MapPlanningContextBundle.from_entries(entries).to_dict()
    except MapPlanningContextError:
        return None


def map_revision_scope_key(target: str, map_layer: int | None = None) -> str:
    """生成与 Godot 前端一致的 canonical 地图 revision 作用域键。

    格式：无图层时直接返回 target；有图层时返回 "target::map_layer=N"，
    确保不同图层的 revision 互不干扰。
    """
    normalized_target = target.strip()
    if map_layer is None:
        return normalized_target
    return f"{normalized_target}::map_layer={map_layer}"


def latest_map_revision(
    session: Session,
    target: str,
    map_layer: int | None = None,
) -> int | None:
    """优先读取目标图层 revision，兼容无图层旧会话记录。

    查找顺序：
    1. 先用 map_revision_scope_key 生成带图层的作用域键，优先匹配
    2. 若未找到且指定了图层，再检查 latest_layers 记录的图层是否匹配
    3. 最后回退到无图层的 target 键（兼容旧会话）
    """
    state: MapTaskState = session.map_task_state
    # 优先查找带图层作用域的 revision
    scoped = state.latest_revisions.get(map_revision_scope_key(target, map_layer))
    if scoped is not None:
        return scoped
    # 若指定了图层但会话记录的图层不匹配，视为无有效 revision
    if map_layer is not None and state.latest_layers.get(target) != map_layer:
        return None
    # 回退到无图层旧格式
    return state.latest_revisions.get(target)


def _target(tool_args: dict[str, Any]) -> str:
    """返回验证调用的目标路径。"""
    value = tool_args.get("target_path", "")
    return value if isinstance(value, str) else ""


def build_map_progress_digest(session: Session, project_root: Path | None = None) -> str:
    """构建精简 map-progress digest，供每轮注入 agent 上下文。

    从权威 map_task_state 派生当前 revision、stage 与最新失败 error_code + repair_plan，
    使关键信息跨压缩存活（不依赖 LLM 摘要）。无活动 map 任务或无失败时返回空串，
    不影响非 map 会话的上下文。
    """
    state = session.map_task_state
    frontier = state.failure_frontier if isinstance(state.failure_frontier, dict) else {}
    revisions = state.latest_revisions
    revision = max(revisions.values()) if revisions else None
    if revision is None and not frontier and not state.planning_contexts:
        return ""
    parts: list[str] = []
    if revision is not None:
        parts.append(f"map_revision={revision}")
    error_code = str(frontier.get("error_code") or frontier.get("blocked_reason") or "")
    if error_code:
        parts.append(f"last_failure={error_code}")
        repair = frontier.get("repair_plan")
        if isinstance(repair, list) and repair:
            parts.append(f"repair_plan={json.dumps(repair[:6], ensure_ascii=False)}")
    snapshots = [
        {
            key: value.get(key)
            for key in (
                "artifact_ref",
                "snapshot_id",
                "digest",
                "target_path",
                "map_layer",
                "map_revision",
                "execution_eligible",
            )
        }
        for value in state.authoritative_snapshots.values()
        if isinstance(value, dict)
    ]
    if snapshots:
        parts.append(
            "planning_snapshots="
            + json.dumps(snapshots[-4:], ensure_ascii=False, separators=(",", ":"))
        )
    contexts = [
        {
            key: value.get(key)
            for key in (
                "context_id",
                "semantic_role",
                "artifact_ref",
                "digest",
                "target_path",
                "map_layer",
                "source_revision",
                "fresh",
            )
        }
        for value in state.planning_contexts.values()
        if isinstance(value, dict)
    ]
    if contexts:
        parts.append(
            "planning_contexts="
            + json.dumps(contexts[-8:], ensure_ascii=False, separators=(",", ":"))
        )
    if state.planning_attempt_history:
        latest_history: list[dict[str, Any]] = next(
            reversed(state.planning_attempt_history.values()), []
        )
        if latest_history:
            parts.append(
                "planning_attempts="
                + json.dumps(latest_history[-3:], ensure_ascii=False, separators=(",", ":"))
            )
    if state.planning_publications:
        latest_publication: dict[str, Any] = next(
            reversed(state.planning_publications.values()), {}
        )
        if latest_publication:
            semantic_value = latest_publication.get("semantic_plan", {})
            semantic = semantic_value if isinstance(semantic_value, dict) else {}
            approved_value = latest_publication.get("approved_batches", [])
            approved = approved_value if isinstance(approved_value, list) else []
            publication_digest = {
                key: latest_publication.get(key)
                for key in (
                    "planning_status",
                    "execution_status",
                    "target_path",
                    "map_layer",
                    "map_revision",
                    "authoritative_snapshot",
                )
            }
            publication_digest["semantic_plan_counts"] = {
                "platforms": len(semantic.get("platforms", [])),
                "segments": len(semantic.get("segments", [])),
                "reference_cells": len(semantic.get("reference_cells", [])),
            }
            publication_digest["approved_batch_refs"] = [
                {
                    "artifact_ref": item.get("artifact_ref"),
                    "batch_id": item.get("batch_id"),
                    "batch_fingerprint": item.get("batch_fingerprint"),
                }
                for item in approved[:12]
                if isinstance(item, dict)
            ]
            parts.append(
                "planning_publication="
                + json.dumps(publication_digest, ensure_ascii=False, separators=(",", ":"))
            )
    if project_root is not None:
        # task 3：注入 map_artifacts.json 的 relative_ref，让 LLM 压缩后能定位持久化的地图工具结果。
        try:
            store = MapArtifactStore(project_root=project_root, session_id=session.session_id)
            parts.append(f"map_artifacts_ref={store.relative_ref}")
        except Exception:  # 路径不可相对化或缺会话信息时跳过（digest 非关键）
            pass
    if not parts:
        return ""
    return "Map progress (authoritative, survives compaction): " + "; ".join(parts) + "."


def _revision(session: Session, tool_args: dict[str, Any]) -> int | None:
    """返回调用声明或会话已知的当前地图 revision。

    优先使用工具参数中的 expected_revision；否则按 target_path + map_layer
    从会话状态中查询（图层感知）。
    """
    value = tool_args.get("expected_revision")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    # 提取 map_layer 参数，用于图层感知的 revision 查询
    layer = tool_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    return latest_map_revision(session, _target(tool_args), map_layer)


def active_planning_snapshot(
    session: Session,
    target_path: str,
    map_layer: int,
) -> dict[str, Any] | None:
    """返回与当前 canonical revision 一致的权威规划快照定位。"""
    scope = planning_snapshot_scope(target_path, map_layer)
    value = session.map_task_state.authoritative_snapshots.get(scope)
    if not isinstance(value, dict):
        return None
    revision = latest_map_revision(session, target_path, map_layer)
    if value.get("map_revision") != revision:
        return None
    if not str(value.get("snapshot_id", "")).strip() or not str(value.get("digest", "")).strip():
        return None
    return deepcopy(value)
