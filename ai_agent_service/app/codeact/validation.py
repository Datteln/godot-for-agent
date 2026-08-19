"""为 CodeAct 写入选择最小且对象匹配的 worker 验证。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from app.codeact.worker import TaskWorker, WorkerManager, WorkerProcessResult
from app.config import AppSettings


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """描述已运行、失败或不可用的目标化验证。"""

    status: str
    verifier: str
    details: str
    exit_code: int | None = None
    version: str = "codeact-validator.v1"

    def to_dict(self) -> dict[str, str | int | None]:
        """转换为 JSON 安全的验证结果。"""
        return asdict(self)


class ValidationSelector:
    """按被修改的项目文件类型选择 worker-only 验证器。"""

    def __init__(self, workers: WorkerManager, settings: AppSettings) -> None:
        self._workers = workers
        self._settings = settings

    async def validate(
        self, worker: TaskWorker, paths: tuple[str, ...], *, timeout_seconds: int
    ) -> ValidationResult:
        """运行单个最相关验证，无法可靠验证时明确返回 unavailable。"""
        suffixes = {PurePosixPath(path).suffix.lower() for path in paths}
        if suffixes & {".gd", ".cs"}:
            return await self._run(
                worker,
                ("godot", "--headless", "--path", "/workspace", "--check-only"),
                "script_static_check",
                timeout_seconds,
            )
        scene = next((path for path in paths if path.lower().endswith(".tscn")), None)
        if scene is not None:
            return await self._resource_probe(worker, scene, "PackedScene", timeout_seconds)
        resource = next((path for path in paths if path.lower().endswith((".tres", ".res"))), None)
        if resource is not None:
            return await self._resource_probe(worker, resource, "Resource", timeout_seconds)
        if suffixes & {".json", ".tmx", ".tilemap"}:
            return ValidationResult(
                "unavailable",
                "map_validator",
                "map validators require a configured project adapter",
            )
        return ValidationResult("unavailable", "none", "no target-matched verifier is configured")

    async def validate_map(
        self,
        worker: TaskWorker,
        paths: tuple[str, ...],
        scope: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> ValidationResult:
        """在 worker 内运行项目配置的地图范围、语义与目标区域校验入口。"""
        command = tuple(self._settings.codeact_map_validator_command)
        if not command:
            return ValidationResult(
                "unavailable",
                "map_range_semantic_target",
                "map validators require codeact_map_validator_command",
            )
        arguments = (
            *command,
            "--codeact-scope-json",
            json.dumps(scope, ensure_ascii=False, sort_keys=True),
            "--changed-paths-json",
            json.dumps(paths, ensure_ascii=False),
        )
        return await self._run(
            worker,
            arguments,
            "map_range_semantic_target",
            timeout_seconds,
        )

    async def _resource_probe(
        self, worker: TaskWorker, path: str, type_name: str, timeout_seconds: int
    ) -> ValidationResult:
        """运行临时 GDScript 加载资源并检查 Godot 基础类型。"""
        probe = "\n".join(
            (
                "extends SceneTree",
                f"var item = ResourceLoader.load('res://{path}')",
                "if item == null:",
                "    quit(2)",
                f"if not is_instance_of(item, {type_name}):",
                "    quit(3)",
                "quit(0)",
            )
        )
        script_path = worker.task_directory / "validate_resource.gd"
        await asyncio.to_thread(script_path.write_text, probe, "utf-8")
        return await self._run(
            worker,
            (
                "godot",
                "--headless",
                "--path",
                "/workspace",
                "--script",
                "/task/validate_resource.gd",
            ),
            f"resource_load:{type_name}",
            timeout_seconds,
        )

    async def _run(
        self, worker: TaskWorker, command: tuple[str, ...], verifier: str, timeout_seconds: int
    ) -> ValidationResult:
        """执行 worker 命令并归一化为可供修复循环消费的结果。"""
        result: WorkerProcessResult = await self._workers.run(
            worker, command, timeout_seconds=timeout_seconds
        )
        details = (result.stderr or result.stdout)[:4000]
        return ValidationResult(
            "passed" if result.exit_code == 0 else "failed", verifier, details, result.exit_code
        )
