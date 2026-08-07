"""解析、校验并应用 Map worker 的结构化完成结果。"""

from __future__ import annotations

import json
from typing import Any

from app.agents.types import Frame
from app.orchestrator.evidence import scoped_evidence
from app.orchestrator.frame_contracts import validate_frame_result
from app.orchestrator.map_contracts import (
    specialized_map_worker_schema,
    validate_map_worker_schema,
)
from app.orchestrator.map_recovery import (
    STRUCTURED_REPAIR_MAX_ATTEMPTS,
    safe_structured_diagnostic,
    structured_repair_actions,
)
from app.orchestrator.map_turn.contracts import logger
from app.orchestrator.map_turn.frame_info import (
    _frame_objective,
    _map_output_schema_for_frame,
    _map_stage_for_frame,
)
from app.orchestrator.map_turn.structured_contracts import (
    MAP_DELEGATE_DROP_KEYS,
    MAP_DELEGATE_LIST_LIMIT,
    MAP_DELEGATE_TEXT_LIMIT,
    MAP_OUTPUT_SCHEMA_V1,
    MAP_WORKER_RESULT_FIELDS,
    MAP_WORKER_STAGE_NAMES,
)
from app.orchestrator.map_workflow import replace_map_state_field
from app.sessions.store import Session


def _normalized_map_layers(payload: dict[str, Any]) -> tuple[int, ...]:
    """把结构化地图结果中的单层或多层标识规整为真实图层索引。"""
    value = payload.get("map_layer")
    if isinstance(value, int) and not isinstance(value, bool):
        return (value,)
    if isinstance(value, list):
        layers = tuple(
            layer for layer in value if isinstance(layer, int) and not isinstance(layer, bool)
        )
        if layers and len(layers) == len(value):
            return tuple(dict.fromkeys(layers))
        return ()
    if not isinstance(value, str) or not value.strip().lower().startswith("all"):
        return ()

    facts = payload.get("facts")
    if not isinstance(facts, list):
        return ()
    indexes: list[int] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        raw_layers = fact.get("layers")
        if not isinstance(raw_layers, list):
            continue
        for layer in raw_layers:
            if not isinstance(layer, dict):
                continue
            index = layer.get("index")
            if isinstance(index, int) and not isinstance(index, bool):
                indexes.append(index)
    return tuple(dict.fromkeys(indexes))


def _normalized_map_layer_value(payload: dict[str, Any]) -> int | list[int] | None:
    """返回适合写回结构化结果的单层或多层值。"""
    layers = _normalized_map_layers(payload)
    if len(layers) == 1:
        return layers[0]
    if layers:
        return list(layers)
    return None


def _json_object_from_text(text: str) -> dict[str, Any] | None:
    """从模型文本中提取 JSON object。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _json_parse_offset(text: str) -> int | None:
    """在 JSON 解析失败时返回安全字符偏移，不保留原始内容。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        json.loads(stripped)
    except json.JSONDecodeError as exc:
        return exc.pos
    return None


def _slim_map_delegate_value(
    value: Any,
    field_name: str = "",
    preserve_lists: bool = False,
) -> Any:
    """递归瘦身地图子任务结果，避免父 agent 继承大数组。

    本轮整改新增 preserve_lists 参数：当 artifact_store 不可用时，
    父帧仍需完整拿到 proposed_batches / write_results 等关键列表，
    此时 preserve_lists=True 跳过列表截断。对于其他字段名则保持
    原有的 MAP_DELEGATE_LIST_LIMIT 截断策略。
    """
    if isinstance(value, str):
        return (
            value
            if len(value) <= MAP_DELEGATE_TEXT_LIMIT
            else value[:MAP_DELEGATE_TEXT_LIMIT] + "..."
        )
    if isinstance(value, list):
        # proposed_batches 和 write_results 是下游编排的关键数据，不可截断
        preserve_lists = preserve_lists or field_name in {
            "proposed_batches",
            "write_results",
        }
        items = value if preserve_lists else value[:MAP_DELEGATE_LIST_LIMIT]
        return [_slim_map_delegate_value(item, preserve_lists=preserve_lists) for item in items]
    if not isinstance(value, dict):
        return value
    slim: dict[str, Any] = {}
    for key, item in value.items():
        key_str = str(key)
        if key_str in MAP_DELEGATE_DROP_KEYS:
            slim[f"{key_str}_omitted"] = True
            continue
        slim[key_str] = _slim_map_delegate_value(item, key_str, preserve_lists)
    return slim


def _map_structured_output_error(
    session: Session,
    frame: Frame,
    text: str,
) -> str | None:
    """校验地图阶段 agent 的 map_worker_result_v1 输出。

    本轮整改大幅增强校验逻辑：
    - stage/target_path/map_revision 必须与 Frame 创建时注入的合同一致；
    - next_stage 必须满足 MAP_WORKER_NEXT_STAGES 定义的合法状态转换；
    - reviewer 阶段必须引用当前 Frame 的截图证据（tool_use_id），
      且截图的 target/revision/region 也要与合同匹配。
    """
    output_schema = _map_output_schema_for_frame(frame)
    if output_schema is None:
        return None
    if output_schema != MAP_OUTPUT_SCHEMA_V1:
        return f"不支持的地图输出 schema：{output_schema}"
    payload = _json_object_from_text(text)
    if payload is None:
        return "输出必须是一个合法 JSON object，schema=map_worker_result_v1。"
    missing = sorted(MAP_WORKER_RESULT_FIELDS - set(payload))
    if missing:
        return "map_worker_result_v1 缺少字段：" + ", ".join(missing)
    schema_errors = validate_map_worker_schema(
        payload,
        specialized_map_worker_schema(frame),
    )
    if schema_errors:
        return "map_worker_result_v1 schema 校验失败：" + "; ".join(schema_errors[:8])
    violations = validate_frame_result(frame, payload)
    if violations:
        violation = violations[0]
        return f"{violation.code}: {violation.message}; {violation.to_dict()}"
    validation = payload.get("validation")
    if not isinstance(validation, dict):
        return "validation 必须是 object。"
    # ── 本轮整改：reviewer 必须引用截图证据，且证据与合同字段保持一致 ──
    if payload.get("stage") == "reviewer":
        raw_evidence_refs = validation.get("evidence_refs")
        evidence_refs = (
            {str(item) for item in raw_evidence_refs}
            if isinstance(raw_evidence_refs, list)
            else set()
        )
        target_for_evidence = str(
            frame.map_stage_contract.get("target_path", payload.get("target_path", ""))
        )
        revision_for_evidence = frame.map_stage_contract.get(
            "map_revision",
            payload.get("map_revision"),
        )
        registered_evidence = (
            scoped_evidence(
                session.map_task_state,
                target_for_evidence,
                revision_for_evidence,
                "viewport_screenshot",
            )
            if isinstance(revision_for_evidence, int)
            and not isinstance(revision_for_evidence, bool)
            else []
        )
        matching_evidence = [
            item
            for item in registered_evidence
            if str(item.get("metadata", {}).get("tool_use_id", "")) in evidence_refs
            and item.get("metadata", {}).get("frame_id") == frame.id
        ]
        if not matching_evidence:
            return "reviewer 必须引用当前 Frame 成功截图的 tool_use_id。"
        contracted_target = frame.map_stage_contract.get("target_path")
        contracted_revision = frame.map_stage_contract.get("map_revision")
        if isinstance(contracted_target, str) and contracted_target:
            if any(item.get("target") != contracted_target for item in matching_evidence):
                return "reviewer 截图证据与当前 Frame 的 target_path 不一致。"
        if isinstance(contracted_revision, int) and not isinstance(contracted_revision, bool):
            if any(item.get("revision") != contracted_revision for item in matching_evidence):
                return "reviewer 截图证据与当前 Frame 的 map_revision 不一致。"
        contracted_region = frame.map_stage_contract.get("region")
        if isinstance(contracted_region, dict) and contracted_region:
            if any(
                item.get("metadata", {}).get("region") != contracted_region
                for item in matching_evidence
            ):
                return "reviewer 截图证据与当前 Frame 的 region 不一致。"
    validation_missing = [
        key for key in ("passed", "issues", "structured_issues") if key not in validation
    ]
    if validation_missing:
        return "validation 缺少字段：" + ", ".join(validation_missing)
    for list_key in ("facts", "proposed_batches", "write_results", "missing_inputs", "risks"):
        if not isinstance(payload.get(list_key), list):
            return f"{list_key} 必须是 array。"
    if _normalized_map_layer_value(payload) is None:
        return (
            "map_layer 必须是整数或非空整数数组；读取全部图层时可使用 "
            '"all"，但 facts 必须包含 layers[].index 作为真实索引依据。'
        )
    return None


def _repair_map_structured_output(
    frame: Frame,
    text: str,
    error: str,
    *,
    category: str,
    attempt: int,
    exhausted: bool,
) -> str:
    """把不合规地图输出保守修复为不可完成的合法结果。"""
    source = _json_object_from_text(text) or {}
    stage = source.get("stage")
    if stage not in MAP_WORKER_STAGE_NAMES:
        stage = _map_stage_for_frame(frame)
    validation = source.get("validation")
    if not isinstance(validation, dict):
        validation = {}
    raw_issues = validation.get("issues")
    issues = list(raw_issues) if isinstance(raw_issues, list) else []
    issues.append(f"structured_output_repaired: {category}")
    raw_structured_issues = validation.get("structured_issues")
    structured_issues = (
        list(raw_structured_issues) if isinstance(raw_structured_issues, list) else []
    )
    structured_issues.append(
        {
            "code": (
                "structured_output_repair_exhausted" if exhausted else "structured_output_repaired"
            ),
            "message": f"structured output rejected ({category})",
            "agent": frame.agent.name,
            "category": category,
            "attempt": attempt,
        }
    )

    def list_value(key: str) -> list[Any]:
        """把指定字段规整为数组。"""
        value = source.get(key)
        if isinstance(value, list):
            return value
        if value is None:
            return []
        return [value]

    map_revision = frame.map_stage_contract.get(
        "map_revision",
        source.get("map_revision"),
    )
    if isinstance(map_revision, bool) or not isinstance(map_revision, int):
        map_revision = 0
    map_layer = _normalized_map_layer_value(source)
    if map_layer is None:
        map_layer = 0
    allowed_next = frame.allowed_next_stages or tuple(
        frame.map_stage_contract.get("allowed_next_stages", ())
    )
    preferred_next = "validator" if stage == "writer" else "planner"
    next_stage = (
        preferred_next
        if preferred_next in allowed_next
        else (allowed_next[0] if allowed_next else stage)
    )
    repaired = {
        "contract_id": frame.contract_id or str(source.get("contract_id") or ""),
        "result_schema": frame.result_schema or MAP_OUTPUT_SCHEMA_V1,
        "stage": stage,
        "worker": frame.worker_instance_id or str(source.get("worker") or frame.agent.name),
        "mode": str(source.get("mode") or "partial"),
        "objective": str(source.get("objective") or _frame_objective(frame)),
        "target_path": str(
            frame.map_stage_contract.get("target_path") or source.get("target_path") or ""
        ),
        "map_layer": map_layer,
        "map_revision": map_revision,
        "region": source.get("region") if isinstance(source.get("region"), dict) else {},
        "summary": str(source.get("summary") or "地图子阶段输出已由服务端保守修复。"),
        "facts": list_value("facts"),
        "proposed_batches": list_value("proposed_batches"),
        "write_results": list_value("write_results"),
        "validation": {
            "passed": False,
            "completion_allowed": False,
            "issues": issues,
            "structured_issues": structured_issues,
        },
        "missing_inputs": list_value("missing_inputs"),
        "risks": [
            *list_value("risks"),
            "结构化输出曾不合规，本结果不能作为任务完成依据。",
        ],
        "next_stage": next_stage,
        "repair": {
            "status": "exhausted" if exhausted else "repaired",
            "error_category": category,
            "original_issue_categories": sorted(
                {
                    category,
                    *(
                        str(item.get("code"))
                        for item in structured_issues
                        if isinstance(item, dict) and item.get("code")
                    ),
                }
            ),
            "safe_diagnostics": [safe_structured_diagnostic(error)],
            "applied_actions": structured_repair_actions(category),
            "attempt": attempt,
            "threshold": STRUCTURED_REPAIR_MAX_ATTEMPTS,
        },
    }
    return json.dumps(repaired, ensure_ascii=False)


def _payload_revision(payload: dict[str, Any]) -> int | None:
    """读取结构化地图结果里的 map_revision。"""
    value = payload.get("map_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _same_payload_target(blocker: dict[str, Any], target: str) -> bool:
    """判断阻断项是否匹配结构化输出的目标地图。"""
    blocker_target = str(blocker.get("target", ""))
    return blocker_target == "" or target == "" or blocker_target == target


def _blocker_required_revision(blocker: dict[str, Any]) -> int | None:
    """读取完成门阻断项要求的 map_revision。"""
    value = blocker.get("required_revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _clear_map_blockers(
    blockers: list[dict[str, Any]],
    target: str,
    revision: int | None,
    reason: str,
) -> list[dict[str, Any]]:
    """清除同目标、同 revision 已满足的地图完成门阻断项。"""
    remaining: list[dict[str, Any]] = []
    for blocker in blockers:
        if blocker.get("reason") != reason:
            remaining.append(blocker)
            continue
        blocker_revision = _blocker_required_revision(blocker)
        if _same_payload_target(blocker, target) and (
            revision is None or blocker_revision is None or revision >= blocker_revision
        ):
            continue
        remaining.append(blocker)
    return remaining


def _append_map_blocker_once(
    blockers: list[dict[str, Any]],
    blocker: dict[str, Any],
) -> list[dict[str, Any]]:
    """追加完成门阻断项，避免重复添加同目标同 revision 同原因条目。"""
    reason = blocker.get("reason")
    target = str(blocker.get("target", ""))
    revision = _blocker_required_revision(blocker)
    for existing in blockers:
        if existing.get("reason") != reason:
            continue
        if not _same_payload_target(existing, target):
            continue
        existing_revision = _blocker_required_revision(existing)
        if revision is None or existing_revision is None or revision == existing_revision:
            return blockers
    return [*blockers, blocker]


def _apply_reader_structured_completion(
    session: Session,
    payload: dict[str, Any],
) -> None:
    """仅在 reader 提交完整事实合同时把地图工作流推进到规划阶段。"""
    target = payload.get("target_path")
    map_layer = _normalized_map_layer_value(payload)
    revision = _payload_revision(payload)
    facts = payload.get("facts")
    missing_inputs = payload.get("missing_inputs")
    mode = str(payload.get("mode", ""))
    complete = (
        mode != "partial"
        and isinstance(target, str)
        and bool(target.strip())
        and map_layer is not None
        and revision is not None
        and isinstance(facts, list)
        and bool(facts)
        and isinstance(missing_inputs, list)
        and not missing_inputs
    )
    state = session.map_task_state
    if complete:
        payload["map_layer"] = map_layer
        # 本轮整改：改用 transition_stage 受控状态机，替代直接赋值 stage
        state.transition_stage("plan")
        replace_map_state_field(
            state,
            "unresolved_issues",
            [],
            target=target,
            revision=revision,
        )
        context_state = dict(state.context_state)
        context_state["reader_result"] = _slim_map_delegate_value(payload)
        context_state.pop("reader_exhausted", None)
        replace_map_state_field(state, "context_state", context_state)
        logger.info(
            "Map reader completion advanced workflow session=%s target=%s layer=%s revision=%s",
            session.session_id,
            target,
            map_layer,
            revision,
        )
        return

    missing = list(missing_inputs) if isinstance(missing_inputs, list) else []
    invalid_fields: list[str] = []
    if not isinstance(target, str) or not target.strip():
        invalid_fields.append("target_path")
    if map_layer is None:
        invalid_fields.append("map_layer")
    if revision is None:
        invalid_fields.append("map_revision")
    if not isinstance(facts, list) or not facts:
        invalid_fields.append("facts")
    if mode == "partial":
        invalid_fields.append("mode=partial")
    if not isinstance(missing_inputs, list):
        invalid_fields.append("missing_inputs")
    # 本轮整改：改用 transition_stage 受控状态机，替代直接赋值 stage
    state.transition_stage("read")
    replace_map_state_field(
        state,
        "unresolved_issues",
        [
            {
                "kind": "reader_incomplete",
                "missing_inputs": missing or invalid_fields,
            }
        ],
        target=target if isinstance(target, str) else None,
        revision=revision,
    )
    logger.info(
        "Map reader completion kept workflow in read stage "
        "session=%s mode=%s missing=%d invalid_fields=%s",
        session.session_id,
        mode,
        len(missing),
        invalid_fields,
    )


def _apply_map_structured_completion_result(session: Session, frame: Frame, text: str) -> None:
    """把地图阶段 agent 的结构化结果合并进工作流状态和完成门。"""
    payload = _json_object_from_text(text)
    if payload is None:
        return
    stage = str(payload.get("stage", ""))
    if stage == "reader":
        _apply_reader_structured_completion(session, payload)
        return
    if stage not in {"validator", "reviewer"}:
        return
    target = str(payload.get("target_path", ""))
    revision = _payload_revision(payload)
    validation = payload.get("validation")
    validation_dict = validation if isinstance(validation, dict) else {}
    observation_passed = (
        validation_dict.get("passed") is True
        and validation_dict.get("blocking_completion") is not True
    )
    issues = validation_dict.get("issues")
    issue_list = [str(issue) for issue in issues] if isinstance(issues, list) else []

    if stage == "validator":
        # 本轮整改：latest_validations 从 session 顶层迁移到 map_task_state
        canonical = session.map_task_state.latest_validations.get(target)
        canonical_matches = (
            isinstance(canonical, dict)
            and bool(target)
            and isinstance(revision, int)
            and not isinstance(revision, bool)
            and canonical.get("target") == target
            and canonical.get("map_revision") == revision
        )
        canonical_success = (
            canonical_matches
            and canonical is not None
            and canonical.get("passed") is True
            and canonical.get("blocking_completion") is not True
        )
        if observation_passed and canonical_success:
            # 本轮整改：completion_blockers 从 session 顶层迁移到 map_task_state
            blockers = _clear_map_blockers(
                session.map_task_state.completion_blockers,
                target,
                revision,
                "map_write_requires_validation",
            )
            blockers = _clear_map_blockers(
                blockers,
                target,
                revision,
                "validator_failed",
            )
            replace_map_state_field(
                session.map_task_state,
                "completion_blockers",
                _append_map_blocker_once(
                    blockers,
                    {
                        "tool": frame.agent.name,
                        "reason": "map_review_required",
                        "issues": [
                            "same-revision validation passed; reviewer visual check is still required"
                        ],
                        "target": target,
                        "required_revision": revision,
                        # 本轮整改：blocker 新增 region 字段，供 reviewer 截图校验比对
                        "region": payload.get("region"),
                    },
                ),
                target=target,
                revision=revision,
            )
        else:
            existing = next(
                (
                    blocker
                    for blocker in session.map_task_state.completion_blockers
                    if blocker.get("target") in ("", target)
                    and (revision is None or blocker.get("required_revision") in (None, revision))
                ),
                None,
            )
            if existing is not None:
                blockers = [
                    {
                        **blocker,
                        **(
                            {"next_stage": blocker.get("next_stage", "planner")}
                            if blocker is existing
                            else {}
                        ),
                    }
                    for blocker in session.map_task_state.completion_blockers
                ]
                replace_map_state_field(
                    session.map_task_state,
                    "completion_blockers",
                    blockers,
                    target=target,
                    revision=revision,
                )
            else:
                blockers = _clear_map_blockers(
                    session.map_task_state.completion_blockers,
                    target,
                    revision,
                    "validator_failed",
                )
                replace_map_state_field(
                    session.map_task_state,
                    "completion_blockers",
                    _append_map_blocker_once(
                        blockers,
                        {
                            "tool": frame.agent.name,
                            "reason": "validator_failed",
                            "issues": issue_list
                            or ["validator failed or no canonical tool validation was recorded"],
                            "target": target,
                            "required_revision": revision,
                            "next_stage": "planner",
                        },
                    ),
                    target=target,
                    revision=revision,
                )
        return

    if observation_passed and not issue_list:
        blockers = _clear_map_blockers(
            session.map_task_state.completion_blockers,
            target,
            revision,
            "map_review_required",
        )
        replace_map_state_field(
            session.map_task_state,
            "completion_blockers",
            _clear_map_blockers(
                blockers,
                target,
                revision,
                "reviewer_failed",
            ),
            target=target,
            revision=revision,
        )
    else:
        blockers = _clear_map_blockers(
            session.map_task_state.completion_blockers,
            target,
            revision,
            "reviewer_failed",
        )
        replace_map_state_field(
            session.map_task_state,
            "completion_blockers",
            _append_map_blocker_once(
                blockers,
                {
                    "tool": frame.agent.name,
                    "reason": "reviewer_failed",
                    "issues": issue_list or ["reviewer observation did not pass"],
                    "target": target,
                    "required_revision": revision,
                },
            ),
            target=target,
            revision=revision,
        )
