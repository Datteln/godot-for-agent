"""地图 worker 结构化结果合同、provider 模式与同 Frame 纠错回归测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition, Frame
from app.config import AppSettings
from app.llm.provider import AssistantTurn, LLMError, OpenAICompatibleProvider, ResponseContract
from app.orchestrator.agent import _finish_frame, _map_structured_output_error, run_turn
from app.orchestrator.map_contracts import (
    MAP_WORKER_RESULT_JSON_SCHEMA_V1,
    MAP_WORKER_RESULT_SCHEMA,
    arm_map_worker_structured_completion,
    map_worker_required_fields,
    specialized_map_worker_schema,
    validate_map_worker_schema,
)
from app.orchestrator.map_progress import (
    MapTaskState,
    _platform_plan_fingerprint,
    _platform_plan_scope,
    build_map_progress_digest,
    map_platform_plan_call_error,
    remember_map_plan_progress,
)
from app.orchestrator.map_recovery import SEMANTIC_RETRY_MAX_ATTEMPTS
from app.orchestrator.map_workflow import replace_map_state_field
from app.sessions.store import Session, session_from_dict, session_to_dict
from app.security.settings import SecuritySettings
from app.tools.context import ToolContext


def _reader_frame(frame_id: str = "f2") -> Frame:
    """构造带冻结 reader 合同的地图 worker Frame。"""
    contract = {
        "contract_id": "contract-1",
        "worker_instance_id": "worker-1",
        "result_schema": MAP_WORKER_RESULT_SCHEMA,
        "stage": "reader",
        "target_path": "Map/Main",
        "map_revision": 7,
        "allowed_next_stages": ["planner", "reader", "replan"],
    }
    agent = AgentDefinition(
        name="dynamic-map-reader",
        source="bundled",
        description="read map",
        prompt="read map",
        pipeline_kind="map",
        role="map_worker",
        map_stage="reader",
        worker_mode="read_only",
    )
    return Frame(
        id=frame_id,
        agent=agent,
        messages=[{"role": "system", "content": "read map"}],
        parent_id="f1",
        map_stage_contract=contract,
        contract_id="contract-1",
        worker_instance_id="worker-1",
        result_schema=MAP_WORKER_RESULT_SCHEMA,
        allowed_next_stages=("planner", "reader", "replan"),
    )


def _valid_reader_result(frame: Frame) -> dict[str, Any]:
    """生成符合 canonical Schema 与冻结 Frame 合同的 reader 结果。"""
    return {
        "contract_id": frame.contract_id,
        "result_schema": MAP_WORKER_RESULT_SCHEMA,
        "stage": "reader",
        "worker": frame.worker_instance_id,
        "mode": "complete",
        "objective": "read map",
        "target_path": "Map/Main",
        "map_layer": [0, 1],
        "map_revision": 7,
        "region": {"x": 0, "y": 0, "width": 10, "height": 6},
        "summary": "facts ready",
        "facts": [{"tile_size": 16}],
        "proposed_batches": [],
        "write_results": [],
        "validation": {
            "passed": True,
            "completion_allowed": False,
            "issues": [],
            "structured_issues": [],
        },
        "missing_inputs": [],
        "risks": [],
        "next_stage": "planner",
    }


def test_canonical_schema_drives_required_and_specialized_constraints() -> None:
    """必填、嵌套类型与 Frame const/enum 均由同一 Schema 校验。"""
    frame = _reader_frame()
    payload = _valid_reader_result(frame)
    schema = specialized_map_worker_schema(frame)

    assert validate_map_worker_schema(payload, schema) == ()
    assert map_worker_required_fields() == frozenset(
        MAP_WORKER_RESULT_JSON_SCHEMA_V1["required"]
    )
    for field_name in map_worker_required_fields():
        invalid = dict(payload)
        invalid.pop(field_name)
        assert any(
            error == f"$.{field_name}: required"
            for error in validate_map_worker_schema(invalid, schema)
        )

    invalid_validation = dict(payload)
    invalid_validation["validation"] = {
        "passed": "yes",
        "issues": {},
        "structured_issues": [],
    }
    nested_errors = validate_map_worker_schema(invalid_validation, schema)
    assert "$.validation.passed: expected_boolean" in nested_errors
    assert "$.validation.issues: expected_array" in nested_errors

    invalid_stage = dict(payload, stage="writer")
    assert "$.stage: const_mismatch" in validate_map_worker_schema(
        invalid_stage,
        schema,
    )
    invalid_next = dict(payload, next_stage="complete")
    assert "$.next_stage: enum_mismatch" in validate_map_worker_schema(
        invalid_next,
        schema,
    )


def test_local_validator_always_applies_frozen_frame_contract() -> None:
    """即使 provider 声称原生 Schema 成功，本地仍拒绝冻结合同错配。"""
    session = Session(session_id="s1", agent_stack=[_reader_frame()])
    payload = _valid_reader_result(session.agent_stack[0])
    assert _map_structured_output_error(session, session.agent_stack[0], json.dumps(payload)) is None

    payload["worker"] = "other-worker"
    assert _map_structured_output_error(
        session,
        session.agent_stack[0],
        json.dumps(payload),
    ) is not None


class _CaptureCompletions:
    """记录 OpenAI SDK create 参数并返回一个空流。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        """保存请求参数并返回可异步遍历的单 chunk 流。"""
        self.calls.append(kwargs)

        async def stream() -> Any:
            """产生一个无 choice 的 usage chunk。"""
            yield SimpleNamespace(choices=[], usage=None)

        return stream()


class _CaptureProvider:
    """记录编排层的逐调用覆盖参数并返回脚本化文本。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    @property
    def supports_tool_calling(self) -> bool:
        """测试 provider 支持工具调用。"""
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        """测试不启用 prompt cache。"""
        return False

    async def chat(
        self,
        _messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AssistantTurn:
        """记录参数并返回预设 assistant 文本。"""
        self.calls.append({"tools": tools, **kwargs})
        return AssistantTurn(
            raw_message={"role": "assistant", "content": self.text},
            content=self.text,
            finish_reason="stop",
            model="test-model",
            response_mode=(
                kwargs["response_contract"].mode
                if kwargs.get("response_contract") is not None
                else None
            ),
        )


class _SequenceProvider(_CaptureProvider):
    """按顺序返回多个脚本化结果，模拟结构化纠错往返。"""

    def __init__(self, texts: list[str]) -> None:
        super().__init__("")
        self.texts = list(texts)

    async def chat(
        self,
        _messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AssistantTurn:
        """消费下一条脚本化结果并记录最终回合参数。"""
        text = self.texts.pop(0)
        self.calls.append({"tools": tools, **kwargs})
        return AssistantTurn(
            raw_message={"role": "assistant", "content": text},
            content=text,
            finish_reason="stop",
            model="test-model",
            response_mode=(
                kwargs["response_contract"].mode
                if kwargs.get("response_contract") is not None
                else None
            ),
        )


@pytest.mark.parametrize(
    ("mode", "expected_format"),
    [
        (
            "json_schema",
            {
                "type": "json_schema",
                "json_schema": {
                    "name": MAP_WORKER_RESULT_SCHEMA,
                    "strict": True,
                    "schema": MAP_WORKER_RESULT_JSON_SCHEMA_V1,
                },
            },
        ),
        ("json_object", {"type": "json_object"}),
        ("prompt_only", None),
    ],
)
def test_provider_builds_each_explicit_response_mode(
    mode: str,
    expected_format: dict[str, Any] | None,
) -> None:
    """三种显式能力模式构造对应请求，并共享同一 wire Schema。"""
    completions = _CaptureCompletions()
    provider = object.__new__(OpenAICompatibleProvider)
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    contract = ResponseContract(
        mode=mode,  # type: ignore[arg-type]
        schema_name=MAP_WORKER_RESULT_SCHEMA,
        schema=MAP_WORKER_RESULT_JSON_SCHEMA_V1,
        fallback_guidance="Wire JSON Schema: {}",
    )

    asyncio.run(
        provider._chat_once(
            [],
            [],
            "model",
            0.0,
            0,
            response_contract=contract,
        )
    )

    assert completions.calls[0]["response_format"] == expected_format
    if mode != "json_schema":
        assert completions.calls[0]["messages"][-1]["content"] == "Wire JSON Schema: {}"


def test_provider_downgrades_response_format_once_before_model_fallback() -> None:
    """格式能力 400 只在同模型降级一次，不触发普通模型 fallback。"""
    provider = object.__new__(OpenAICompatibleProvider)
    provider._default_model = "primary"  # type: ignore[attr-defined]
    provider._fallback_model = "fallback"  # type: ignore[attr-defined]
    calls: list[tuple[str, str]] = []

    async def fake_chat_once(
        _messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]],
        model: str,
        *_args: Any,
        response_contract: ResponseContract | None = None,
    ) -> AssistantTurn:
        """拒绝 json_schema，接受同模型 json_object。"""
        mode = response_contract.mode if response_contract is not None else "none"
        calls.append((model, mode))
        if mode == "json_schema":
            raise LLMError("unsupported response format", status_code=400)
        return AssistantTurn(
            raw_message={"role": "assistant", "content": "{}"},
            content="{}",
            model=model,
        )

    provider._chat_once = fake_chat_once  # type: ignore[method-assign]
    result = asyncio.run(
        provider.chat(
            [],
            [],
            response_contract=ResponseContract(
                mode="json_schema",
                schema_name=MAP_WORKER_RESULT_SCHEMA,
                schema=MAP_WORKER_RESULT_JSON_SCHEMA_V1,
                fallback_guidance="Wire JSON Schema: {}",
            ),
        )
    )

    assert result.model == "primary"
    assert calls == [("primary", "json_schema"), ("primary", "json_object")]


def test_response_contract_and_deterministic_overrides_only_reach_final_turn() -> None:
    """普通 worker 回合保持原策略，force_text_only 回合才收到专用合同。"""
    security = SecuritySettings(project_root=Path.cwd())
    tool_ctx = ToolContext(
        security=security,
        session_id="s1",
        session_epoch="e1",
    )

    intermediate = _reader_frame("f1")
    intermediate.parent_id = None
    intermediate.agent.effective_tools.append("read_file")
    normal_provider = _CaptureProvider("not a final structured result")
    asyncio.run(
        run_turn(
            Session(session_id="s1", agent_stack=[intermediate]),
            normal_provider,
            security,
            tool_ctx,
            max_turns=1,
        )
    )
    assert normal_provider.calls[0]["response_contract"] is None
    assert normal_provider.calls[0]["temperature"] != 0.0

    final = _reader_frame("f1")
    final.parent_id = None
    arm_map_worker_structured_completion(
        final,
        mode="json_schema",
        correction_limit=1,
    )
    final_provider = _CaptureProvider(
        json.dumps(_valid_reader_result(final), ensure_ascii=False)
    )
    asyncio.run(
        run_turn(
            Session(session_id="s2", agent_stack=[final]),
            final_provider,
            security,
            ToolContext(
                security=security,
                session_id="s2",
                session_epoch="e1",
            ),
            max_turns=1,
            map_worker_response_contract_mode="json_schema",
            map_worker_structured_thinking_budget=128,
        )
    )
    final_call = final_provider.calls[0]
    assert final_call["tools"] == []
    assert final_call["temperature"] == 0.0
    assert final_call["thinking_budget"] == 128
    assert final_call["response_contract"].mode == "json_schema"
    assert final_call["response_contract"].schema["properties"]["stage"]["const"] == "reader"


def test_parallel_final_turns_keep_response_modes_and_overrides_isolated() -> None:
    """并行 worker 的模式与最终回合覆盖保留在各自 Frame/调用内。"""
    security = SecuritySettings(project_root=Path.cwd())
    frames = [_reader_frame("fa"), _reader_frame("fb")]
    modes = ["json_schema", "prompt_only"]
    providers: list[_CaptureProvider] = []
    sessions: list[Session] = []
    for index, (frame, mode) in enumerate(zip(frames, modes, strict=True)):
        frame.parent_id = None
        frame.worker_instance_id = f"worker-{index}"
        frame.contract_id = f"contract-{index}"
        frame.map_stage_contract["worker_instance_id"] = frame.worker_instance_id
        frame.map_stage_contract["contract_id"] = frame.contract_id
        arm_map_worker_structured_completion(
            frame,
            mode=mode,  # type: ignore[arg-type]
            correction_limit=1,
        )
        provider = _CaptureProvider(
            json.dumps(_valid_reader_result(frame), ensure_ascii=False)
        )
        providers.append(provider)
        sessions.append(Session(session_id=f"s{index}", agent_stack=[frame]))

    async def run_parallel() -> None:
        """并发驱动两个独立 Session。"""
        await asyncio.gather(
            *(
                run_turn(
                    session,
                    provider,
                    security,
                    ToolContext(
                        security=security,
                        session_id=session.session_id,
                        session_epoch="e1",
                    ),
                    max_turns=1,
                    map_worker_response_contract_mode=mode,  # type: ignore[arg-type]
                )
                for session, provider, mode in zip(
                    sessions,
                    providers,
                    modes,
                    strict=True,
                )
            )
        )

    asyncio.run(run_parallel())

    assert [provider.calls[0]["response_contract"].mode for provider in providers] == modes
    assert all(provider.calls[0]["temperature"] == 0.0 for provider in providers)
    assert all(provider.calls[0]["tools"] == [] for provider in providers)


@pytest.mark.parametrize(
    "invalid_text",
    [
        '{"stage":"reader"}',
        "not-json",
        json.dumps(
            {
                **_valid_reader_result(_reader_frame()),
                "worker": "wrong-worker",
            }
        ),
    ],
)
def test_scripted_invalid_results_succeed_without_repeating_map_reads(
    invalid_text: str,
) -> None:
    """缺字段、坏 JSON、合同错配均在同一 Frame 的下一次最终调用纠正。"""
    frame = _reader_frame("f1")
    frame.parent_id = None
    frame.messages.append(
        {
            "role": "tool",
            "tool_call_id": "read-1",
            "content": '{"facts":"already gathered"}',
        }
    )
    provider = _SequenceProvider(
        [
            "facts gathered; ready to serialize",
            invalid_text,
            json.dumps(_valid_reader_result(frame), ensure_ascii=False),
        ]
    )
    security = SecuritySettings(project_root=Path.cwd())

    asyncio.run(
        run_turn(
            Session(session_id="s1", agent_stack=[frame]),
            provider,
            security,
            ToolContext(
                security=security,
                session_id="s1",
                session_epoch="e1",
            ),
            max_turns=3,
        )
    )

    assert len(provider.calls) == 3
    assert provider.calls[0]["response_contract"] is None
    assert all(call["response_contract"] is not None for call in provider.calls[1:])
    assert all(call["tools"] == [] for call in provider.calls)
    assert any(message.get("tool_call_id") == "read-1" for message in frame.messages)


def test_invalid_result_is_corrected_inside_same_frame() -> None:
    """首次无效结果不发布、不弹 Frame，也不增加任务级语义计数。"""
    parent = Frame(
        id="f1",
        agent=get_agent("coordinator", set()),
        messages=[],
    )
    child = _reader_frame()
    arm_map_worker_structured_completion(
        child,
        mode="prompt_only",
        correction_limit=1,
    )
    session = Session(session_id="s1", agent_stack=[parent, child])

    first = asyncio.run(_finish_frame(session, '{"stage":"reader"}'))

    assert first is None
    assert session.top_frame() is child
    assert child.structured_attempt_count == 1
    assert session.map_task_state.retry_registry == {}
    correction = str(child.messages[-1]["content"])
    assert "frozen_constraints" in correction
    assert '{"stage":"reader"}' not in correction

    second = asyncio.run(
        _finish_frame(
            session,
            json.dumps(_valid_reader_result(child), ensure_ascii=False),
        )
    )

    assert second is None
    assert session.top_frame() is parent
    assert session.map_task_state.retry_registry == {}


def test_local_exhaustion_records_one_semantic_failure() -> None:
    """本地额度耗尽后才保守修复，且一个失败 Frame 只计一次语义失败。"""
    parent = Frame(
        id="f1",
        agent=get_agent("coordinator", set()),
        messages=[],
    )
    child = _reader_frame()
    arm_map_worker_structured_completion(
        child,
        mode="prompt_only",
        correction_limit=1,
    )
    session = Session(session_id="s1", agent_stack=[parent, child])

    asyncio.run(_finish_frame(session, "not-json"))
    asyncio.run(_finish_frame(session, "still-not-json"))

    assert session.top_frame() is parent
    assert len(session.map_task_state.retry_registry) == 1
    retry = next(iter(session.map_task_state.retry_registry.values()))
    assert retry["attempt"] == 1


def test_replacement_workers_share_semantic_budget_and_pause_resumably() -> None:
    """等价替换 worker 共享计数；类别、revision 和不同操作保持隔离。"""
    parent = Frame(
        id="f1",
        agent=get_agent("coordinator", set()),
        messages=[],
    )
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
    )
    session = Session(
        session_id="s1",
        agent_stack=[parent],
        map_task_state=state,
    )
    attempts: list[int] = []
    for index in range(3):
        child = _reader_frame(f"worker-frame-{index}")
        child.parent_id = parent.id
        child.worker_instance_id = f"worker-instance-{index}"
        child.contract_id = f"contract-{index}"
        child.map_stage_contract["worker_instance_id"] = child.worker_instance_id
        child.map_stage_contract["contract_id"] = child.contract_id
        arm_map_worker_structured_completion(
            child,
            mode="prompt_only",
            correction_limit=0,
        )
        session.agent_stack.append(child)
        asyncio.run(_finish_frame(session, "not-json"))
        matching = [
            item
            for item in state.retry_registry.values()
            if item["error_category"] == "invalid_json"
        ]
        attempts.append(int(matching[0]["attempt"]))

    assert attempts == [1, 2, 3]
    assert state.status == "paused"
    assert state.pause_report["type"] == "map_retry_exhausted"
    assert state.pause_report["first_root_cause"]
    assert state.pause_report["last_attempt"]["attempt"] == 3
    assert state.pause_report["recovery_guidance"]

    state.cancel("test task epoch reset")
    state.start_new_task("task-2", lineage_id="lineage-2")
    assert state.retry_registry == {}


def test_changed_categories_revisions_and_operations_use_independent_retry_keys() -> None:
    """语义重试身份区分错误类别、地图 revision 与真正不同的并行操作。"""
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
    )
    parent = Frame(
        id="f1",
        agent=get_agent("coordinator", set()),
        messages=[],
    )
    session = Session(
        session_id="s1",
        agent_stack=[parent],
        map_task_state=state,
    )
    cases = [
        ("not-json", 7, "read collision"),
        ('{"stage":"reader"}', 7, "read collision"),
        ("not-json", 8, "read collision"),
        ("not-json", 7, "read navigation"),
    ]
    for index, (text, revision, objective) in enumerate(cases):
        child = _reader_frame(f"f{index + 2}")
        child.map_stage_contract["map_revision"] = revision
        child.messages.append({"role": "user", "content": objective})
        child.structured_correction_limit = 0
        child.force_text_only = True
        session.agent_stack.append(child)
        asyncio.run(_finish_frame(session, text))

    assert len(state.retry_registry) == 4


def test_provisional_invalid_result_cannot_mutate_map_task_state() -> None:
    """仍有本地额度的临时无效结果不能推进任何 reducer-owned 地图状态。"""
    parent = Frame(
        id="f1",
        agent=get_agent("coordinator", set()),
        messages=[],
    )
    child = _reader_frame()
    arm_map_worker_structured_completion(
        child,
        mode="prompt_only",
        correction_limit=1,
    )
    state = MapTaskState(
        task_id="task-1",
        task_lineage_id="lineage-1",
        status="running",
        stage="read",
    )
    session = Session(
        session_id="s1",
        agent_stack=[parent, child],
        map_task_state=state,
    )
    before = state.to_dict()

    asyncio.run(_finish_frame(session, '{"stage":"reader"}'))

    assert state.to_dict() == before
    assert session.top_frame() is child


def test_legacy_frame_defaults_structured_metadata_on_restore() -> None:
    """旧持久化 Frame 缺少新增字段时按兼容默认值恢复。"""
    session = Session(
        session_id="s1",
        agent_stack=[
            Frame(
                id="f1",
                agent=get_agent("coordinator", set()),
                messages=[],
            )
        ],
    )
    payload = session_to_dict(session)
    frame_payload = payload["agent_stack"][0]
    for key in (
        "structured_attempt_count",
        "structured_correction_limit",
        "response_contract_mode",
        "response_contract_schema_digest",
        "structured_response_model",
        "structured_finish_reason",
        "structured_thinking_budget",
        "structured_diagnostics",
    ):
        frame_payload.pop(key, None)

    restored = session_from_dict(payload, set()).agent_stack[0]

    assert restored.structured_attempt_count == 0
    assert restored.structured_correction_limit == 0
    assert restored.response_contract_mode is None
    assert restored.response_contract_schema_digest is None
    assert restored.structured_response_model is None
    assert restored.structured_finish_reason is None
    assert restored.structured_thinking_budget == 0
    assert restored.structured_diagnostics == []


def _orchestrator_frame(frame_id: str = "f2") -> Frame:
    """构造带冻结 orchestrator 合同的 map-agent Frame，模拟总控完成回合。

    复现日志中 stage='orchestrator' 被特化 schema 的静态 enum 误杀的现场：
    基础 schema 的 stage enum 不含 'orchestrator'，但 const 被钉成 'orchestrator'。
    """
    contract = {
        "contract_id": "frame-contract:orchestrator-1",
        "worker_instance_id": "worker-instance:session-1:f2",
        "result_schema": MAP_WORKER_RESULT_SCHEMA,
        "stage": "orchestrator",
        "target_path": "TileMap",
        "map_revision": 0,
        "allowed_next_stages": [],
    }
    agent = AgentDefinition(
        name="map-agent",
        source="bundled",
        description="map orchestrator",
        prompt="orchestrate map",
        pipeline_kind="map",
        role="map_worker",
        map_stage="orchestrator",
        worker_mode="read_only",
    )
    return Frame(
        id=frame_id,
        agent=agent,
        messages=[{"role": "system", "content": "orchestrate map"}],
        parent_id="f1",
        map_stage_contract=contract,
        contract_id="frame-contract:orchestrator-1",
        worker_instance_id="worker-instance:session-1:f2",
        result_schema=MAP_WORKER_RESULT_SCHEMA,
        allowed_next_stages=(),
    )


def _valid_orchestrator_result(frame: Frame) -> dict[str, Any]:
    """生成与冻结 orchestrator 合同一致、字段齐全的 map-agent 结果。"""
    return {
        "contract_id": frame.contract_id,
        "result_schema": MAP_WORKER_RESULT_SCHEMA,
        "stage": "orchestrator",
        "worker": frame.worker_instance_id,
        "mode": "complete",
        "objective": "orchestrate map",
        "target_path": "TileMap",
        "map_layer": [0, 1],
        "map_revision": 0,
        "region": {},
        "summary": "map context ready",
        "facts": ["tile_map_path=TileMap"],
        "proposed_batches": [],
        "write_results": [],
        "validation": {
            "passed": True,
            "completion_allowed": True,
            "issues": [],
            "structured_issues": [],
        },
        "missing_inputs": [],
        "risks": [],
        "next_stage": "planner",
    }


def test_orchestrator_stage_passes_specialized_schema() -> None:
    """orchestrator 帧正确输出 stage='orchestrator' 不再被静态 enum 误判。"""
    frame = _orchestrator_frame()
    payload = _valid_orchestrator_result(frame)
    schema = specialized_map_worker_schema(frame)

    errors = validate_map_worker_schema(payload, schema)

    assert not any(error.startswith("$.stage") for error in errors), errors
    assert errors == (), errors


def test_specialized_const_not_in_base_enum_is_admissible() -> None:
    """const 钉死字段值时丢弃 enum：const 值不在基础 enum 内也应当可满足。"""
    frame = _orchestrator_frame()
    schema = specialized_map_worker_schema(frame)
    stage_prop = schema["properties"]["stage"]

    assert stage_prop.get("const") == "orchestrator"
    assert "enum" not in stage_prop

    base_stage_enum = MAP_WORKER_RESULT_JSON_SCHEMA_V1["properties"]["stage"]["enum"]
    assert "orchestrator" not in base_stage_enum


def test_orchestrator_result_not_false_rejected_at_schema_level() -> None:
    """回归：orchestrator 帧的正确输出不再触发 stage 误判（forced_validation_failure 根因）。"""
    frame = _orchestrator_frame()
    payload = _valid_orchestrator_result(frame)
    schema = specialized_map_worker_schema(frame)

    assert not any(
        "stage" in error for error in validate_map_worker_schema(payload, schema)
    )


def test_platform_plan_call_error_no_count_cap_only_fingerprint_dedup() -> None:
    """修订计数上限已移除：distinct 方案不再因计数被拒；仅相同指纹去重。"""
    session = Session(session_id="s1", agent_stack=[_reader_frame()])
    base_args = {
        "target_path": "TileMap",
        "map_layer": 1,
        "platforms": [{"id": "p0", "x": 51, "y": -4, "width": 7, "role": "safe_intro"}],
        "segments": [
            {"index": 0, "type": "walk", "start": {"x": 51, "y": -5}, "end": {"x": 52, "y": -5}}
        ],
    }
    scope = _platform_plan_scope(base_args)
    fp = _platform_plan_fingerprint("validate_platform_level_plan", base_args)
    assert fp is not None  # platforms/segments 齐全，应生成指纹

    # 模拟该方案已提交过（指纹已记录）且 attempts 已很高
    session.map_task_state.planning_attempts[scope] = 5
    session.map_task_state.planning_fingerprints[f"{scope}::{fp}"] = 1

    # 相同方案再提交 → 被指纹去重拒绝（而非计数上限）
    duplicate_msg = map_platform_plan_call_error(
        session, "validate_platform_level_plan", base_args
    )
    assert duplicate_msg is not None
    assert "已经校验过" in duplicate_msg

    # distinct 方案（不同 platforms）→ 即便 attempts=5 也不被拒（计数上限已移除）
    distinct_args = {
        **base_args,
        "platforms": [{"id": "p0", "x": 51, "y": -4, "width": 9, "role": "safe_intro"}],
        "segments": [
            {"index": 0, "type": "walk", "start": {"x": 51, "y": -5}, "end": {"x": 53, "y": -5}}
        ],
    }
    distinct_msg = map_platform_plan_call_error(
        session, "validate_platform_level_plan", distinct_args
    )
    assert distinct_msg is None


def test_build_map_progress_digest_empty_without_map_state() -> None:
    """无活动 map 进度（无 revision、无 failure_frontier）时 digest 为空，不污染非 map 上下文。"""
    session = Session(session_id="s1", agent_stack=[_reader_frame()])
    assert build_map_progress_digest(session) == ""


def test_build_map_progress_digest_surfaces_failure_and_repair_plan() -> None:
    """digest 从权威 state 派生 revision + 失败 error_code + repair_plan（跨压缩存活）。"""
    session = Session(session_id="s1", agent_stack=[_reader_frame()])
    state = session.map_task_state
    replace_map_state_field(
        state, "latest_revisions", {"TileMap::1": 7}, target="TileMap", revision=7
    )
    replace_map_state_field(
        state,
        "failure_frontier",
        {
            "error_code": "route_endpoint_geometry_mismatch",
            "blocked_reason": "segment endpoint off by one",
            "repair_plan": [
                {"path": "segments[0].start", "action": "Place endpoint above platform."}
            ],
        },
        target="TileMap",
        revision=7,
    )
    digest = build_map_progress_digest(session)
    assert "map_revision=7" in digest
    assert "route_endpoint_geometry_mismatch" in digest
    assert "repair_plan=" in digest
    assert "survives compaction" in digest


def test_correction_floor_two_allows_second_correction() -> None:
    """correction floor=2：首次纠错仍无效时还有第二次纠错机会，不立即 fail-closed。"""
    parent = Frame(id="f1", agent=get_agent("coordinator", set()), messages=[])
    child = _reader_frame()
    arm_map_worker_structured_completion(child, mode="prompt_only", correction_limit=2)
    session = Session(session_id="s1", agent_stack=[parent, child])

    # 首次无效 → 安排第 1 次纠错
    first = asyncio.run(_finish_frame(session, '{"stage":"reader"}'))
    assert first is None
    assert child.structured_attempt_count == 1

    # 第 1 次纠错仍无效 → 仍有第 2 次纠错机会（未 fail-closed 回退到 parent）
    second = asyncio.run(_finish_frame(session, '{"stage":"reader"}'))
    assert second is None
    assert child.structured_attempt_count == 2
    assert session.top_frame() is child


def test_final_structured_turn_thinking_budget_falls_back_to_effort_tier() -> None:
    """param=0 时最终结构化回合回退到 effort 档 thinking（非零），不再饿死最难输出任务。"""
    final = _reader_frame("f1")
    final.parent_id = None
    arm_map_worker_structured_completion(final, mode="json_schema", correction_limit=1)
    final_provider = _CaptureProvider(
        json.dumps(_valid_reader_result(final), ensure_ascii=False)
    )
    security = SecuritySettings(project_root=Path.cwd())
    asyncio.run(
        run_turn(
            Session(session_id="s2", agent_stack=[final]),
            final_provider,
            security,
            ToolContext(security=security, session_id="s2", session_epoch="e1"),
            max_turns=1,
            map_worker_response_contract_mode="json_schema",
            # map_worker_structured_thinking_budget 默认 0 → 应回退到 effort 档（非零）
            thinking_budget_selector=lambda _effort: 7777,
        )
    )
    final_call = final_provider.calls[0]
    assert final_call["thinking_budget"] == 7777
    assert final_call["thinking_budget"] != 0


def test_build_map_progress_digest_surfaces_map_artifacts_ref() -> None:
    """task 3：digest 注入 map_artifacts.json 的 relative_ref，让 LLM 压缩后能定位 artifact store。"""
    session = Session(session_id="s1", agent_stack=[_reader_frame()])
    state = session.map_task_state
    replace_map_state_field(
        state, "failure_frontier", {"error_code": "x"}, target="TileMap", revision=7
    )
    digest = build_map_progress_digest(session, project_root=Path.cwd())
    assert "map_artifacts_ref=" in digest


def test_successful_non_platform_plan_advances_to_write_despite_sibling_pending() -> None:
    """Decision 6：plan_map_layout 成功即进 write，不再因 sibling pending planner workflow 滞留 plan。

    旧版在成功路径用 ``if tool_name not in PLATFORM_PLAN_TOOL_NAMES`` 守卫扫描同 target+revision
    的 sibling workflow（next_stage=='planner'），命中即 ``transition_stage('plan')`` 把整任务滞留于
    规划阶段。该守卫已移除：通过客观校验的非平台规划应推进到 write，sibling 的再规划由通用 no-progress
    语义重试与每 scope 写入门独立兜底。本测试用 sibling 精确命中旧 ``locked_scope`` 的条件，钉住新行为
    （旧版此处 stage 会是 'plan'）。
    """
    session = Session(session_id="s1", agent_stack=[_reader_frame()])
    state = session.map_task_state
    target = "TileMap"
    # 当前 scope（map_layer=0）权威 revision=7（reducer-owned 字段须经 replace_map_state_field 写入）
    replace_map_state_field(
        state,
        "latest_revisions",
        {"TileMap::map_layer=0": 7},
        target=target,
        revision=7,
    )
    # sibling scope（同 target、不同 layer）留一个 pending planner workflow——
    # map_revision==current_revision(7)、next_stage=='planner'，正是旧 locked_scope 会据此
    # transition_stage('plan') 的精确条件。
    sibling_scope = "TileMap::map_layer=1"
    replace_map_state_field(
        state,
        "validation_workflows",
        {
            sibling_scope: {
                "map_revision": 7,
                "next_stage": "planner",
                "plan_tool": "validate_platform_level_plan",
            }
        },
        target=target,
        revision=7,
    )

    # plan_map_layout 成功（非 platform tool：executable 仅要求 ok 且无 blocked_reason/error_code）
    remember_map_plan_progress(
        session,
        "plan_map_layout",
        {"target_path": target, "map_layer": 0},
        {"target": target, "map_layer": 0, "ok": True},
    )

    # Decision 6：推进到 write，而非滞留 plan（旧版此处会是 'plan'）。
    assert state.stage == "write", state.stage
    # sibling 的 pending planner 状态不被触碰（只是不再阻断本 scope 推进）。
    assert state.validation_workflows[sibling_scope]["next_stage"] == "planner"


def test_production_settings_supply_correction_floor_of_two() -> None:
    """9.2：生产设置不显式覆盖 correction-limit 时默认≥2，worker 能拿到第二次本地纠错。

    构造无 correction-limit 覆盖的生产设置，把其 correction floor 原样送入 run_turn；
    worker 首回合中间文本后连续两次无效结构化结果仍能继续纠错，第 4 次才给出有效结果。
    只有 correction floor≥2 才会出现第 4 次调用（floor=1 时第 1 次纠错失败即
    fail-closed，只会产生 3 次调用），从而证明生产默认值把纠错底线抬到至少 2。
    """
    settings = AppSettings(_env_file=None)
    assert settings.map_worker_structured_correction_limit >= 2

    frame = _reader_frame("f1")
    frame.parent_id = None
    frame.messages.append(
        {
            "role": "tool",
            "tool_call_id": "read-1",
            "content": '{"facts":"already gathered"}',
        }
    )
    provider = _SequenceProvider(
        [
            "facts gathered; ready to serialize",
            '{"stage":"reader"}',
            '{"stage":"reader"}',
            json.dumps(_valid_reader_result(frame), ensure_ascii=False),
        ]
    )
    security = SecuritySettings(project_root=Path.cwd())
    asyncio.run(
        run_turn(
            Session(session_id="s1", agent_stack=[frame]),
            provider,
            security,
            ToolContext(security=security, session_id="s1", session_epoch="e1"),
            max_turns=1,
            map_worker_structured_output_enabled=settings.map_worker_structured_output_enabled,
            map_worker_response_contract_mode=settings.map_worker_response_contract_mode,
            map_worker_structured_correction_limit=settings.map_worker_structured_correction_limit,
        )
    )
    # 生产设置值原样流入 Frame 的本地纠错上限。
    assert frame.structured_correction_limit == settings.map_worker_structured_correction_limit
    # 4 次调用 = 首回合中间文本 + 两次纠错（第 2 次纠错存在即证明 floor≥2）+ 有效回合。
    assert len(provider.calls) == 4


def test_structured_thinking_budget_fallback_consistent_across_surfaces() -> None:
    """9.6：param=0 回退到 effort 档时，provider 参数、frame 证据、session 序列化与
    结构化诊断载荷四处报告同一个非零有效 thinking 预算。

    无效回合触发结构化诊断（记录当时 frame.structured_thinking_budget），随后有效回合
    收尾；四个观测面——两次 provider 调用参数、frame.structured_thinking_budget、持久化
    往返后的 frame、structured_diagnostics 载荷——应同为 7777。
    """
    final = _reader_frame("f1")
    final.parent_id = None
    arm_map_worker_structured_completion(final, mode="json_schema", correction_limit=2)
    provider = _SequenceProvider(
        [
            '{"stage":"reader"}',
            json.dumps(_valid_reader_result(final), ensure_ascii=False),
        ]
    )
    security = SecuritySettings(project_root=Path.cwd())
    session = Session(session_id="s2", agent_stack=[final])
    asyncio.run(
        run_turn(
            session,
            provider,
            security,
            ToolContext(security=security, session_id="s2", session_epoch="e1"),
            max_turns=1,
            map_worker_response_contract_mode="json_schema",
            map_worker_structured_thinking_budget=0,
            thinking_budget_selector=lambda _effort: 7777,
        )
    )
    # provider 参数：两次最终回合都用同一非零回退预算。
    assert [call["thinking_budget"] for call in provider.calls] == [7777, 7777]
    # frame 证据：最终回合写入的有效预算。
    assert final.structured_thinking_budget == 7777
    # 结构化诊断载荷：无效回合记录的预算与上面一致。
    assert final.structured_diagnostics[-1]["thinking_budget"] == 7777
    # session 序列化：持久化载荷中 frame 的有效预算与上面一致（直接读载荷，避免
    # 动态 worker agent 名不在 bundled registry 中导致反序列化 KeyError）。
    payload = session_to_dict(session)
    assert payload["agent_stack"][0]["structured_thinking_budget"] == 7777


def test_failed_non_platform_plan_does_not_touch_platform_validation_streak() -> None:
    """9.8：plan_map_layout 失败不记入 platform-validation 重试 streak；平台规划失败才累积并可耗尽。

    非平台规划工具（plan_map_layout）失败时 remember_map_plan_progress 直接返回 None，
    不记 validation_failure 语义重试（retry_registry 仍为空）；随后平台规划工具
    （validate_platform_level_plan）失败从 attempt=1 起算、不受前者影响，重复 N 次后耗尽。
    """
    session = Session(session_id="s1", agent_stack=[_reader_frame()])
    state = session.map_task_state
    target = "TileMap"
    replace_map_state_field(
        state,
        "latest_revisions",
        {"TileMap::map_layer=0": 7},
        target=target,
        revision=7,
    )
    plan_args = {"target_path": target, "map_layer": 0}
    failing = {
        "target": target,
        "map_layer": 0,
        "ok": False,
        "blocked_reason": "plan_not_executable",
        "error_code": "plan_not_executable",
    }

    # 非平台规划失败：不记 validation_failure 语义重试，直接返回 None。
    non_platform = remember_map_plan_progress(session, "plan_map_layout", plan_args, failing)
    assert non_platform is None
    assert state.retry_registry == {}

    platform_args = {
        "target_path": target,
        "map_layer": 0,
        "platforms": [{"id": "p0", "x": 51, "y": -4, "width": 3, "role": "safe_intro"}],
        "segments": [
            {"index": 0, "type": "walk", "start": {"x": 51, "y": -5}, "end": {"x": 52, "y": -5}}
        ],
    }
    platform_failing = {
        "target": target,
        "map_layer": 0,
        "ok": False,
        "blocked_reason": "platform_plan_failed",
        "error_code": "platform_plan_failed",
    }
    attempts: list[int] = []
    entry: dict[str, Any] = {}
    for _ in range(SEMANTIC_RETRY_MAX_ATTEMPTS):
        entry = remember_map_plan_progress(
            session, "validate_platform_level_plan", platform_args, platform_failing
        )
        assert entry is not None
        attempts.append(int(entry["attempt"]))
    # 平台 streak 从 1 起算（非平台失败未污染），重复 N 次后耗尽。
    assert attempts == list(range(1, SEMANTIC_RETRY_MAX_ATTEMPTS + 1))
    assert bool(entry["exhausted"]) is True
    # retry_registry 只累积一条 validation_failure streak（非平台失败未贡献）。
    assert len(state.retry_registry) == 1
