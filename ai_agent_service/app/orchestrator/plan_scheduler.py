"""不可变计划 DAG 与确定性步骤调度。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, cast

from app.orchestrator.runtime_contracts import PlanStepResult, PlanStepStatus

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "blocked", "cancelled"})
_NON_SUCCESS_TERMINAL_STATUSES = frozenset({"failed", "blocked", "cancelled"})


class PlanGraphError(ValueError):
    """表示计划 DAG、输入绑定或状态转换不合法。"""


@dataclass(frozen=True)
class PlanInputBinding:
    """把前置步骤输出的一个字段绑定为后继步骤输入。"""

    name: str
    source_step_id: str
    source_path: str = ""
    required: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlanInputBinding:
        """从持久化结构恢复输入绑定。"""
        name = str(value.get("name", "")).strip()
        source_step_id = str(value.get("source_step_id", "")).strip()
        if not name or not source_step_id:
            raise PlanGraphError("input binding requires name and source_step_id")
        return cls(
            name=name,
            source_step_id=source_step_id,
            source_path=str(value.get("source_path", "")).strip(),
            required=value.get("required") is not False,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 原生结构。"""
        return {
            "name": self.name,
            "source_step_id": self.source_step_id,
            "source_path": self.source_path,
            "required": self.required,
        }


@dataclass(frozen=True)
class PlanStep:
    """保存稳定 id、依赖、输入合同和终态结果的不可变计划步骤。"""

    step_id: str
    order: int
    title: str
    agent: str
    task: str
    depends_on: tuple[str, ...] = ()
    input_bindings: tuple[PlanInputBinding, ...] = ()
    expected_result_schema: dict[str, Any] | None = None
    estimated_complexity: str | None = None
    worker_spec: dict[str, Any] | None = None
    status: PlanStepStatus = "pending"
    frame_id: str | None = None
    result: PlanStepResult | None = None
    bound_inputs: dict[str, Any] | None = None
    recovery_attempt: int = 0

    @classmethod
    def from_dict(cls, value: dict[str, Any], order: int) -> PlanStep:
        """从创建参数或持久化记录恢复步骤。"""
        step_id = str(value.get("id", value.get("step_id", f"step-{order + 1}"))).strip()
        if not step_id:
            step_id = f"step-{order + 1}"
        bindings_value = value.get("input_bindings", [])
        bindings = tuple(
            PlanInputBinding.from_dict(item)
            for item in bindings_value
            if isinstance(item, dict)
        )
        raw_result = value.get("result")
        result = None
        if isinstance(raw_result, dict):
            result = PlanStepResult(
                status=_coerce_status(raw_result.get("status"), "failed"),
                output=(
                    dict(raw_result.get("output", {}))
                    if isinstance(raw_result.get("output"), dict)
                    else {}
                ),
                artifact_refs=tuple(
                    str(item)
                    for item in raw_result.get("artifact_refs", [])
                    if isinstance(item, str)
                ),
                error_code=(
                    str(raw_result["error_code"])
                    if raw_result.get("error_code") is not None
                    else None
                ),
                blocked_by=tuple(
                    str(item)
                    for item in raw_result.get("blocked_by", [])
                    if isinstance(item, str)
                ),
            )
        expected_schema = value.get("expected_result_schema")
        return cls(
            step_id=step_id,
            order=order,
            title=str(value.get("title", "")).strip(),
            agent=str(value.get("agent", "")).strip(),
            task=str(value.get("task", "")).strip(),
            depends_on=tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in value.get("depends_on", [])
                    if isinstance(item, str) and item.strip()
                )
            ),
            input_bindings=bindings,
            expected_result_schema=(
                dict(expected_schema) if isinstance(expected_schema, dict) else None
            ),
            estimated_complexity=(
                str(value["estimated_complexity"])
                if value.get("estimated_complexity") is not None
                else None
            ),
            worker_spec=(
                dict(value["worker_spec"])
                if isinstance(value.get("worker_spec"), dict)
                else None
            ),
            status=_coerce_status(value.get("status"), "pending"),
            frame_id=(
                str(value["frame_id"]) if value.get("frame_id") is not None else None
            ),
            result=result,
            bound_inputs=(
                dict(value["bound_inputs"])
                if isinstance(value.get("bound_inputs"), dict)
                else None
            ),
            recovery_attempt=int(value.get("recovery_attempt", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化步骤记录。"""
        return {
            "id": self.step_id,
            "title": self.title,
            "agent": self.agent,
            "task": self.task,
            "depends_on": list(self.depends_on),
            "input_bindings": [binding.to_dict() for binding in self.input_bindings],
            "expected_result_schema": self.expected_result_schema,
            "estimated_complexity": self.estimated_complexity,
            "worker_spec": self.worker_spec,
            "status": self.status,
            "frame_id": self.frame_id,
            "result": self.result.to_dict() if self.result is not None else None,
            "bound_inputs": self.bound_inputs,
            "recovery_attempt": self.recovery_attempt,
        }


@dataclass(frozen=True)
class PlanGraph:
    """保存计划摘要和拓扑稳定的不可变步骤集合。"""

    summary: str
    steps: tuple[PlanStep, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlanGraph:
        """恢复并完整验证一个计划 DAG。"""
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanGraphError("plan steps cannot be empty")
        graph = cls(
            summary=str(value.get("summary", "")).strip(),
            steps=tuple(
                PlanStep.from_dict(item, index)
                for index, item in enumerate(raw_steps)
                if isinstance(item, dict)
            ),
        )
        graph._validate()
        return graph

    def _validate(self) -> None:
        """校验步骤字段、依赖引用与 DAG 无环性。"""
        if not self.steps:
            raise PlanGraphError("plan steps cannot be empty")
        ids = [step.step_id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise PlanGraphError("plan step ids must be unique")
        known = set(ids)
        for step in self.steps:
            if not step.title or not step.agent or not step.task:
                raise PlanGraphError("plan step requires title, agent and task")
            unknown = set(step.depends_on) - known
            if unknown:
                raise PlanGraphError(
                    f"plan step {step.step_id} has unknown dependencies: {sorted(unknown)}"
                )
            if step.step_id in step.depends_on:
                raise PlanGraphError(f"plan step {step.step_id} depends on itself")
            for binding in step.input_bindings:
                if binding.source_step_id not in step.depends_on:
                    raise PlanGraphError(
                        f"input binding source {binding.source_step_id} must be a dependency "
                        f"of {step.step_id}"
                    )

        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {step.step_id: step for step in self.steps}

        def visit(step_id: str) -> None:
            """深度优先检测依赖环。"""
            if step_id in visiting:
                raise PlanGraphError("plan dependencies must form a DAG")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in by_id[step_id].depends_on:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)

    def to_dict(self) -> dict[str, Any]:
        """转换为 Session 可直接持久化的计划。"""
        return {
            "schema_version": 2,
            "summary": self.summary,
            "steps": [step.to_dict() for step in self.steps],
            "frame_steps": {
                step.frame_id: step.step_id
                for step in self.steps
                if step.frame_id is not None
            },
        }

    def runnable_steps(self) -> tuple[PlanStep, ...]:
        """按原计划顺序返回所有依赖均成功的 pending 步骤。"""
        by_id = {step.step_id: step for step in self.steps}
        return tuple(
            step
            for step in self.steps
            if step.status == "pending"
            and all(by_id[item].status == "succeeded" for item in step.depends_on)
        )

    def start(self, step_id: str, frame_id: str) -> PlanGraph:
        """返回把指定 runnable 步骤转换为 running 的新计划。"""
        runnable = {step.step_id for step in self.runnable_steps()}
        if step_id not in runnable:
            raise PlanGraphError(f"plan step is not runnable: {step_id}")
        return self._replace_step(
            step_id,
            replace(
                self.step(step_id),
                status="running",
                frame_id=frame_id,
                bound_inputs=self.bind_inputs(step_id),
            ),
        )

    def finish(self, step_id: str, result: PlanStepResult) -> PlanGraph:
        """返回写入终态结果并传播依赖阻断的新计划。"""
        step = self.step(step_id)
        if step.status != "running":
            raise PlanGraphError(f"plan step is not running: {step_id}")
        graph = self._replace_step(
            step_id,
            replace(step, status=result.status, result=result),
        )
        return graph._propagate_blocked()

    def fail_unstarted(self, step_id: str, error_code: str) -> PlanGraph:
        """把无法创建 Frame 的 pending 步骤标记失败并传播阻断。"""
        step = self.step(step_id)
        if step.status != "pending":
            raise PlanGraphError(f"plan step is not pending: {step_id}")
        result = PlanStepResult(
            status="failed",
            error_code=error_code,
        )
        graph = self._replace_step(
            step_id,
            replace(step, status="failed", result=result),
        )
        return graph._propagate_blocked()

    def inject_reader_recovery(
        self,
        step_id: str,
        *,
        missing_inputs: list[Any],
        target: str,
        revision: int,
    ) -> PlanGraph:
        """在 running 步骤前插入 reader，并把类型化结果绑定到原步骤重试。"""
        original = self.step(step_id)
        if original.status != "running":
            raise PlanGraphError(f"plan step is not running: {step_id}")
        attempt = original.recovery_attempt + 1
        reader_id = f"{step_id}__reader__attempt_{attempt}"
        if any(step.step_id == reader_id for step in self.steps):
            raise PlanGraphError(f"reader recovery step already exists: {reader_id}")
        reader = PlanStep(
            step_id=reader_id,
            order=len(self.steps),
            title=f"Recover missing inputs for {original.title}",
            agent="map-worker",
            task=(
                "Read only the canonical facts referenced by this recovery input: "
                f"{missing_inputs!r}; target={target!r}; revision={revision}."
            ),
            depends_on=original.depends_on,
            expected_result_schema={
                "type": "object",
                "required": [
                    "stage",
                    "target_path",
                    "map_revision",
                    "missing_inputs",
                ],
            },
            worker_spec={
                "name": f"reader-recovery-{attempt}",
                "objective": "Read canonical facts required by a blocked map step.",
                "mode": "read_only",
                "operations": ["describe_map_region", "read_file"],
                "constraints": [],
                "output_schema": "map_worker_result_v1",
                "stage_id": "reader",
                "max_turns": 4,
                "recovery_request": {
                    "missing_inputs": missing_inputs,
                    "target_path": target,
                    "map_revision": revision,
                    "attempt": attempt,
                },
            },
        )
        retry_binding = PlanInputBinding(
            name="recovery_facts",
            source_step_id=reader_id,
            source_path="",
            required=True,
        )
        retry = replace(
            original,
            status="pending",
            frame_id=None,
            result=None,
            depends_on=tuple((*original.depends_on, reader_id)),
            input_bindings=tuple((*original.input_bindings, retry_binding)),
            bound_inputs=None,
            recovery_attempt=attempt,
        )
        graph = PlanGraph(
            summary=self.summary,
            steps=tuple(
                retry if step.step_id == step_id else step for step in self.steps
            )
            + (reader,),
        )
        graph._validate()
        return graph

    def step(self, step_id: str) -> PlanStep:
        """按稳定 id 返回步骤。"""
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise PlanGraphError(f"unknown plan step: {step_id}")

    def bind_inputs(self, step_id: str) -> dict[str, Any]:
        """从已成功前置步骤提取类型化结果和 artifact 引用。"""
        step = self.step(step_id)
        values: dict[str, Any] = {}
        if not step.input_bindings:
            for dependency_id in step.depends_on:
                dependency = self.step(dependency_id)
                if dependency.result is not None:
                    values[dependency_id] = dependency.result.to_dict()
            return values
        for binding in step.input_bindings:
            dependency = self.step(binding.source_step_id)
            source: Any = (
                dependency.result.to_dict()
                if dependency.result is not None
                else None
            )
            value = _read_path(source, binding.source_path)
            if value is None and binding.required:
                raise PlanGraphError(
                    f"required input {binding.name} missing from {binding.source_step_id}"
                )
            values[binding.name] = value
        return values

    def task_payload(self, step_id: str) -> dict[str, Any]:
        """生成只含调度器已解锁步骤的委派参数。"""
        step = self.step(step_id)
        payload: dict[str, Any] = {
            "agent": step.agent,
            "task": step.task,
            "plan_step_id": step.step_id,
            "scheduler_inputs": self.bind_inputs(step_id),
        }
        if step.worker_spec is not None:
            payload["worker_spec"] = dict(step.worker_spec)
        return payload

    def is_terminal(self) -> bool:
        """判断全部步骤是否已进入终态。"""
        return all(step.status in _TERMINAL_STATUSES for step in self.steps)

    def _replace_step(self, step_id: str, replacement: PlanStep) -> PlanGraph:
        """返回替换单个步骤后的新图。"""
        return PlanGraph(
            summary=self.summary,
            steps=tuple(
                replacement if step.step_id == step_id else step
                for step in self.steps
            ),
        )

    def _propagate_blocked(self) -> PlanGraph:
        """把失败、取消或阻断的前置终态传播给尚未启动的后继步骤。"""
        graph = self
        changed = True
        while changed:
            changed = False
            by_id = {step.step_id: step for step in graph.steps}
            replacements: dict[str, PlanStep] = {}
            for step in graph.steps:
                if step.status != "pending":
                    continue
                blocked_by = tuple(
                    dependency
                    for dependency in step.depends_on
                    if by_id[dependency].status in _NON_SUCCESS_TERMINAL_STATUSES
                )
                if not blocked_by:
                    continue
                result = PlanStepResult(
                    status="blocked",
                    error_code="predecessor_not_succeeded",
                    blocked_by=blocked_by,
                )
                replacements[step.step_id] = replace(
                    step,
                    status="blocked",
                    result=result,
                )
                changed = True
            if replacements:
                graph = PlanGraph(
                    summary=graph.summary,
                    steps=tuple(replacements.get(step.step_id, step) for step in graph.steps),
                )
        return graph


def _coerce_status(value: Any, default: PlanStepStatus) -> PlanStepStatus:
    """把外部状态规整为合法计划状态。"""
    if value in {
        "pending",
        "running",
        "succeeded",
        "failed",
        "blocked",
        "cancelled",
    }:
        return cast(PlanStepStatus, value)
    return default


def _read_path(source: Any, path: str) -> Any:
    """按点分路径读取嵌套字典；空路径返回整个值。"""
    if not path:
        return source
    current = source
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current
