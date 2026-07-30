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
from app.orchestrator.map_progress import MapTaskState
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
