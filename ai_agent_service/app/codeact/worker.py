"""通过 rootless Docker 执行任务范围内的隔离命令。"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import AppSettings
from app.security.paths import ProjectRoots

_CONTAINER_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,120}$")


class WorkerUnavailableError(RuntimeError):
    """表示 Docker worker 尚不能安全执行请求。"""


@dataclass(frozen=True, slots=True)
class WorkerProcessResult:
    """描述一个独立 worker 进程的受限输出。"""

    exit_code: int
    stdout: str
    stderr: str
    output_truncated: bool


@dataclass(slots=True)
class TaskWorker:
    """保存一个任务复用容器与隔离缓存卷的生命周期。"""

    task_execution_id: str
    container_name: str
    cache_volume_name: str
    task_directory: Path
    roots: ProjectRoots
    started: bool = False


class WorkerManager:
    """管理每个执行任务唯一且可回收的 rootless Docker worker。"""

    def __init__(self, settings: AppSettings, task_root: Path) -> None:
        self._settings = settings
        self._task_root = task_root
        self._workers: dict[str, TaskWorker] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, task_execution_id: str, roots: ProjectRoots) -> TaskWorker:
        """取得或创建任务唯一的隔离容器与 `.godot` cache volume。"""
        async with self._lock:
            existing = self._workers.get(task_execution_id)
            if existing is not None:
                return existing
            safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "-", task_execution_id)[:80]
            container_name = f"codeact-{safe_id}"
            volume_name = f"codeact-godot-{safe_id}"
            if not _CONTAINER_NAME.fullmatch(container_name):
                raise WorkerUnavailableError("task execution id cannot form a safe worker name")
            task_directory = self._task_root / safe_id
            await asyncio.to_thread(task_directory.mkdir, parents=True, exist_ok=True)
            worker = TaskWorker(
                task_execution_id, container_name, volume_name, task_directory, roots
            )
            try:
                await self._start(worker)
            except BaseException:
                await self._cleanup(worker)
                raise
            self._workers[task_execution_id] = worker
            return worker

    async def run(
        self, worker: TaskWorker, command: tuple[str, ...], *, timeout_seconds: int
    ) -> WorkerProcessResult:
        """在复用容器内启动独立命令进程，且绝不退回宿主 Shell。"""
        if not worker.started:
            raise WorkerUnavailableError("worker is not running")
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "--workdir",
            "/workspace",
            worker.container_name,
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        return self._result(process.returncode, stdout, stderr)

    async def cancel(self, task_execution_id: str) -> None:
        """停止任务容器并清理其临时目录与 cache volume。"""
        worker = self._workers.pop(task_execution_id, None)
        if worker is not None:
            await self._cleanup(worker)

    async def close(self) -> None:
        """清理当前服务持有的全部任务 worker。"""
        workers = tuple(self._workers.values())
        self._workers.clear()
        for worker in workers:
            await self._cleanup(worker)

    def execution_ids(self) -> tuple[str, ...]:
        """返回当前由管理器持有的 worker 执行标识。"""
        return tuple(self._workers)

    async def _start(self, worker: TaskWorker) -> None:
        """创建禁止网络与提权的非 root 长寿命容器。"""
        await self._prepare_cache_volume(worker)
        command = _worker_start_command(worker, self._settings)
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(
                process.communicate(), self._settings.codeact_worker_timeout_s
            )
        except (FileNotFoundError, TimeoutError) as exc:
            raise WorkerUnavailableError("rootless Docker worker is unavailable") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[:400]
            raise WorkerUnavailableError(f"worker startup failed: {detail}")
        worker.started = True

    async def _prepare_cache_volume(self, worker: TaskWorker) -> None:
        """创建任务缓存卷并在受限 init 容器中授予 worker 写权限。"""
        commands = (
            ("docker", "volume", "create", worker.cache_volume_name),
            (
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,src={worker.cache_volume_name},dst=/cache",
                self._settings.codeact_worker_image,
                "chown",
                "10001:10001",
                "/cache",
            ),
        )
        for command in commands:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    process.communicate(), self._settings.codeact_worker_timeout_s
                )
            except (FileNotFoundError, TimeoutError) as exc:
                raise WorkerUnavailableError("rootless Docker worker is unavailable") from exc
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[:400]
                raise WorkerUnavailableError(f"worker cache initialization failed: {detail}")

    async def _cleanup(self, worker: TaskWorker) -> None:
        """尽力删除容器、volume 和任务临时目录。"""
        for command in (
            ("docker", "rm", "--force", worker.container_name),
            ("docker", "volume", "rm", "--force", worker.cache_volume_name),
        ):
            with contextlib.suppress(FileNotFoundError, OSError):
                process = await asyncio.create_subprocess_exec(
                    *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), 15)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(_remove_task_directory, worker.task_directory)
        worker.started = False

    def _result(self, exit_code: int, stdout: bytes, stderr: bytes) -> WorkerProcessResult:
        """按配置截断进程输出，避免无限日志进入 agent 上下文。"""
        limit = self._settings.codeact_worker_output_bytes
        truncated = len(stdout) + len(stderr) > limit
        stdout_slice = stdout[:limit]
        remaining = max(0, limit - len(stdout_slice))
        return WorkerProcessResult(
            exit_code,
            stdout_slice.decode("utf-8", errors="replace"),
            stderr[:remaining].decode("utf-8", errors="replace"),
            truncated,
        )


def _remove_task_directory(path: Path) -> None:
    """仅删除已由 WorkerManager 创建的单个任务目录。"""
    if path.parent.name != "codeact":
        raise OSError("refusing to remove a task directory outside the codeact root")
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _worker_start_command(worker: TaskWorker, settings: AppSettings) -> tuple[str, ...]:
    """构造带只读根、可写临时目录和只读 Git 元数据的 worker 启动参数。"""
    git_metadata = worker.roots.resolved_project_root / ".git"
    git_mount = (
        (
            "--mount",
            f"type=bind,src={git_metadata},dst=/workspace/.git,readonly",
        )
        if worker.roots.repository_root == worker.roots.resolved_project_root
        and git_metadata.exists()
        else ()
    )
    return (
        "docker",
        "run",
        "--detach",
        "--name",
        worker.container_name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10001:10001",
        "--cpus",
        str(settings.codeact_worker_cpu),
        "--memory",
        f"{settings.codeact_worker_memory_mb}m",
        "--pids-limit",
        str(settings.codeact_worker_pids_limit),
        "--env",
        "HOME=/home/codeact",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m,uid=10001,gid=10001",
        "--tmpfs",
        "/home/codeact:rw,nosuid,nodev,size=64m,uid=10001,gid=10001",
        "--mount",
        f"type=bind,src={worker.roots.resolved_project_root},dst=/workspace",
        *git_mount,
        "--mount",
        f"type=volume,src={worker.cache_volume_name},dst=/workspace/.godot",
        "--mount",
        f"type=bind,src={worker.task_directory},dst=/task",
        settings.codeact_worker_image,
        "sleep",
        "infinity",
    )
