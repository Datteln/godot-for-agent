"""创建单个 Map 委派 Frame 并绑定领域合同。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.orchestrator.frame_factory import create_child_frame
from app.orchestrator.map_contracts import (
    MAP_RUNTIME_STAGE_TRANSITIONS,
    MAP_WORKER_STAGES,
    MAP_WORKER_TO_RUNTIME_STAGE,
)
from app.orchestrator.map_planning_contexts import (
    MapPlanningContextBundle,
    MapPlanningContextEntry,
    MapPlanningContextError,
)
from app.orchestrator.map_progress import (
    # 本轮整改：revision 查询改为图层感知，避免跨图层 revision 冲突
    latest_map_revision,
    record_map_child_lineage,
)
from app.orchestrator.map_turn.contracts import (
    MAX_AGENT_DEPTH,
    AgentPromptFactory,
    _tool_message,
    logger,
)
from app.orchestrator.map_turn.frame_info import (
    _find_frame,
    _frame_in_active_map_edit,
)
from app.orchestrator.map_turn.planning import _plan_step_started
from app.orchestrator.map_turn.tool_dispatch import _map_stage_contract
from app.orchestrator.map_workers import (
    # 本轮整改：验证工具名与 mode→stage 映射表集中定义，避免硬编码散落
    build_dynamic_map_worker,
    is_map_worker_write_mode,
)
from app.sessions.store import Session
from app.tools.registry import REGISTRY


async def _delegate_child_frame(
    *,
    session: Session,
    parent_id: str,
    call_id: str | None,
    group_id: str | None,
    args: dict[str, Any],
    depth: int,
    prompt_factory: AgentPromptFactory | None,
) -> Frame | str | None:
    """根据委派参数创建子 agent 帧，并保留动态 worker 的具体错误。

    本轮整改要点：
    - 改用 ``agent.role`` / ``agent.map_stage`` 做权限判断，不再硬编码 agent name；
    - 写阶段切换延迟到 prompt/skill 绑定成功后（``transition_stage``），
      避免 worker 创建失败时污染 ``map_task_state.stage``；
    - 子帧创建统一委托给 ``frame_factory.create_child_frame``，
      由其负责 history_anchor 继承与 stage_contract 注入。
    """
    agent_name = args.get("agent")
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return None
    parent = _find_frame(session, parent_id)
    worker_spec = args.get("worker_spec")
    if isinstance(worker_spec, dict):
        if agent_name not in {"map-worker", "map-agent"}:
            return (
                "动态地图 worker 必须使用 agent=map-worker；"
                "不得把 worker_spec 与永久 Agent 名组合"
            )
        # 本轮整改：用 map_stage 代替硬编码 name=="map-agent"，
        # 使 capability contract 派生的动态 agent 也能成为合法父帧
        if parent is None or parent.agent.map_stage != "orchestrator":
            return "只有 map-agent 可以创建动态地图 worker"
        worker_spec = dict(worker_spec)
        worker_spec.setdefault(
            "stage_id",
            f"{group_id or call_id or parent_id}:{worker_spec.get('mode', 'stage')}",
        )
        worker_spec.setdefault("lifecycle_scope", "delegate_frame")
        worker_mode_value = str(worker_spec.get("mode", ""))
        if worker_mode_value in {"propose_only", "repair_propose"}:
            try:
                raw_bundle = worker_spec.get("planning_context_bundle")
                if isinstance(raw_bundle, dict):
                    bundle = MapPlanningContextBundle.from_dict(raw_bundle)
                else:
                    entries: list[MapPlanningContextEntry] = []
                    for value in session.map_task_state.planning_contexts.values():
                        if not isinstance(value, dict):
                            continue
                        entry = MapPlanningContextEntry.from_dict(value)
                        if not entry.fresh:
                            continue
                        if (
                            entry.target_path is not None
                            and entry.map_layer is not None
                            and entry.source_revision is not None
                            and latest_map_revision(session, entry.target_path, entry.map_layer)
                            != entry.source_revision
                        ):
                            continue
                        entries.append(entry)
                    if not entries:
                        entries = [
                            MapPlanningContextEntry.from_snapshot(value)
                            for value in session.map_task_state.authoritative_snapshots.values()
                            if isinstance(value, dict)
                            and isinstance(value.get("snapshot_id"), str)
                            and isinstance(value.get("digest"), str)
                            and isinstance(value.get("target_path"), str)
                            and isinstance(value.get("map_layer"), int)
                            and isinstance(value.get("map_revision"), int)
                            and latest_map_revision(
                                session,
                                str(value["target_path"]),
                                int(value["map_layer"]),
                            )
                            == value.get("map_revision")
                        ]
                    required_roles = worker_spec.get("required_context_roles", [])
                    bundle = MapPlanningContextBundle.from_entries(
                        entries,
                        required_roles=(required_roles if isinstance(required_roles, list) else ()),
                    )
            except MapPlanningContextError as exc:
                return f"planner_context_binding_failed：{exc}"
            worker_spec["planning_context_bundle"] = bundle.to_dict()
            if len(bundle.contexts) == 1:
                entry = bundle.contexts[0]
                legacy_snapshot = next(
                    (
                        dict(value)
                        for value in session.map_task_state.authoritative_snapshots.values()
                        if isinstance(value, dict)
                        and value.get("snapshot_id") == entry.context_id
                        and value.get("digest") == entry.digest
                    ),
                    None,
                )
                if legacy_snapshot is not None:
                    # 单上下文保持旧合同的只读别名，供滚动升级中的调用方读取；
                    # planner 的事实源仍是 planning_context_bundle。
                    worker_spec.setdefault("authoritative_snapshot", legacy_snapshot)
        requested_worker_name = str(worker_spec.get("name", "")).strip()
        if requested_worker_name:
            try:
                get_agent(requested_worker_name, set(REGISTRY))
            except KeyError:
                pass
            else:
                return "动态 worker 名称与永久 Agent 定义冲突：" f"{requested_worker_name}"
        child_or_error = build_dynamic_map_worker(parent.agent, worker_spec)
        if isinstance(child_or_error, str):
            return child_or_error
        reserved_identity = (
            f"__map_worker__:{session.session_id}:"
            f"{worker_spec['stage_id']}:{requested_worker_name}"
        )
        child_agent = replace(
            child_or_error,
            name=reserved_identity,
            description=(f"{child_or_error.description}; display_name={requested_worker_name}"),
        )
    else:
        if not isinstance(agent_name, str) or not agent_name:
            return None
        try:
            child_agent = get_agent(agent_name, set(REGISTRY))
        except KeyError:
            return None
        # 本轮整改：改用 role=="map_orchestrator" 做递归守卫，
        # 不再依赖 agent name 字符串匹配
        if (
            parent is not None
            and parent.agent.role == "map_orchestrator"
            and child_agent.role == "map_orchestrator"
        ):
            return (
                "地图总控不得递归委派另一个地图总控；"
                "写入请创建带 worker_spec.mode=write_one_batch 的动态地图 worker"
            )
    task_text = task.strip()
    scheduler_inputs = args.get("scheduler_inputs")
    if isinstance(scheduler_inputs, dict) and scheduler_inputs:
        task_text = (
            f"{task_text}\n\n"
            "[SCHEDULER_INPUTS]\n"
            f"{json.dumps(scheduler_inputs, ensure_ascii=False, sort_keys=True, default=str)}"
        )
    child_task_stage = (
        MAP_WORKER_TO_RUNTIME_STAGE.get(str(child_agent.map_stage))
        if child_agent.pipeline_kind == "map"
        else None
    )
    expected_task_stage = session.map_task_state.stage
    if child_task_stage is not None:
        allowed_task_stages = MAP_RUNTIME_STAGE_TRANSITIONS.get(expected_task_stage, frozenset())
        if child_task_stage not in allowed_task_stages:
            return (
                "map_child_stage_transition_blocked："
                f"{expected_task_stage} -> {child_task_stage}"
            )
    if is_map_worker_write_mode(child_agent.worker_mode) and not _frame_in_active_map_edit(
        session, parent
    ):
        return "当前用户请求未显式授权地图内容编辑，不能创建写入 worker"
    try:
        prompt = (
            await prompt_factory(child_agent, task_text)
            if prompt_factory is not None
            else child_agent.prompt
        )
    except ValueError as exc:
        # 本轮整改：prompt_factory 失败时直接返回错误，不再创建残缺子帧
        return str(exc)
    child_agent = replace(child_agent, prompt=prompt)
    if parent is None:
        return None
    # 本轮整改：子帧创建统一走 frame_factory，自动继承 history_anchor
    # 并注入 _map_stage_contract 用于后续输出一致性校验
    child = create_child_frame(
        session=session,
        parent=parent,
        agent=child_agent,
        task_text=task_text,
        depth=depth,
        pending_delegate_call_id=call_id,
        pending_delegate_group_id=group_id,
        map_stage_contract=_map_stage_contract(child_agent, task_text, worker_spec),
    )
    child_stage = child.map_stage_contract.get("stage")
    if (
        parent.agent.role == "map_orchestrator"
        and isinstance(child_stage, str)
        and child_stage in MAP_WORKER_STAGES
    ):
        target = str(child.map_stage_contract.get("target_path") or "")
        revision = child.map_stage_contract.get("map_revision")
        child_bundle = child.map_stage_contract.get("planning_context_bundle")
        child_operations = child.map_stage_contract.get("execution_operations")
        record_map_child_lineage(
            session.map_task_state,
            child_frame_id=child.id,
            child_stage=child_stage,
            task_stage=child_task_stage or expected_task_stage,
            expected_task_stage=expected_task_stage,
            target=target,
            revision=(
                revision if isinstance(revision, int) and not isinstance(revision, bool) else None
            ),
            planning_context_bundle_id=(
                str(child_bundle.get("bundle_id"))
                if isinstance(child_bundle, dict) and isinstance(child_bundle.get("bundle_id"), str)
                else None
            ),
            planning_context_bundle=(
                dict(child_bundle) if isinstance(child_bundle, dict) else None
            ),
            execution_operations=(
                [dict(item) for item in child_operations if isinstance(item, dict)]
                if isinstance(child_operations, list)
                else []
            ),
        )
    return child


async def _start_delegate_frame(
    *,
    session: Session,
    frame: Frame,
    call_id: str,
    args: dict[str, Any],
    prompt_factory: AgentPromptFactory | None,
    event_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> bool:
    """创建子 agent 帧并压栈，成功时返回 True。"""
    agent_name = args.get("agent")
    task = args.get("task")
    has_worker_spec = isinstance(args.get("worker_spec"), dict)
    if not has_worker_spec and (not isinstance(agent_name, str) or not agent_name):
        logger.warning(
            "Delegate rejected: missing agent session=%s frame=%s", session.session_id, frame.id
        )
        frame.messages.append(_tool_message(call_id, "delegate.agent 不能为空", is_error=True))
        return False
    if not isinstance(task, str) or not task.strip():
        logger.warning(
            "Delegate rejected: missing task session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            agent_name,
        )
        frame.messages.append(_tool_message(call_id, "delegate.task 不能为空", is_error=True))
        return False
    if not frame.agent.can_delegate:
        logger.warning(
            "Delegate rejected: agent cannot delegate session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(
            _tool_message(call_id, "当前 agent 不允许委派子 agent", is_error=True)
        )
        return False
    # 本轮整改：用 map_stage=="orchestrator" 代替 name=="map-agent"
    if has_worker_spec and frame.agent.map_stage != "orchestrator":
        logger.warning(
            "Delegate rejected: dynamic worker parent is not map-agent session=%s frame=%s agent=%s",
            session.session_id,
            frame.id,
            frame.agent.name,
        )
        frame.messages.append(
            _tool_message(call_id, "只有 map-agent 可以创建动态地图 worker", is_error=True)
        )
        return False
    if frame.depth >= MAX_AGENT_DEPTH:
        logger.warning(
            "Delegate rejected: max depth session=%s frame=%s depth=%d",
            session.session_id,
            frame.id,
            frame.depth,
        )
        frame.messages.append(
            _tool_message(call_id, "已达到最大委派深度，不能继续创建子 agent", is_error=True)
        )
        return False

    child = await _delegate_child_frame(
        session=session,
        parent_id=frame.id,
        call_id=call_id,
        group_id=None,
        args=args,
        depth=frame.depth + 1,
        prompt_factory=prompt_factory,
    )
    if isinstance(child, str):
        logger.warning(
            "Delegate rejected: invalid child session=%s frame=%s agent=%s error=%s",
            session.session_id,
            frame.id,
            agent_name,
            child,
        )
        frame.messages.append(_tool_message(call_id, child, is_error=True))
        return False
    if child is None:
        logger.warning(
            "Delegate rejected: unknown child agent session=%s agent=%s",
            session.session_id,
            agent_name,
        )
        frame.messages.append(_tool_message(call_id, f"未知子 agent：{agent_name}", is_error=True))
        return False
    session.agent_stack.append(child)
    _plan_step_started(session, child, event_callback)
    logger.info(
        "Delegate frame started session=%s parent_frame=%s child_frame=%s parent_agent=%s child_agent=%s depth=%d",
        session.session_id,
        frame.id,
        child.id,
        frame.agent.name,
        child.agent.name,
        child.depth,
    )
    return True
