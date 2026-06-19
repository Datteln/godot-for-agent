"""上下文缓存的"决定性输入"指纹计算（§16.1）。

百炼的显式缓存通过 `cache_control` content-block 在服务端按 token 前缀
匹配，并不接受客户端传入的缓存键——这里算出的指纹只用于本服务内部：判断
"这一帧的稳定前缀自上一轮以来是否变化"，供 `cache_decision_engine.py`
分类日志/指标，不会出现在发给端点的请求体里，也不影响端点真实的缓存行为。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from app.llm.message_transformer import flatten_message_text

logger = logging.getLogger(__name__)

# repo 指纹的本地缓存有效期（秒）：避免同一会话内每次 LLM 调用都重新拉取
# git 状态。这是纯内部观测信号、不是正确性依赖，短暂滞后可接受。
_REPO_FINGERPRINT_TTL_S = 15.0
_GIT_TIMEOUT_S = 2.0

# 计入 dependency_lock_hash 的常见清单/锁文件（存在才计入）：它们变化通常意味着
# 工程依赖/结构发生了实质改动，值得让缓存键失效。Godot 工程以 project.godot
# 为主，其余为 Python/Node 生态的常见锁文件，便于该服务被复用到混合工程。
_DEPENDENCY_LOCK_FILES = (
    "project.godot",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)


def compute_tool_schema_version(tools: list[dict[str, Any]]) -> str:
    """对当前可见工具的 schema 做稳定序列化哈希。

    百炼把工具定义序列化为 JSON 参与缓存前缀计算，且要求顺序/字段顺序稳定
    才能命中；这里保持 `tools` 原始顺序与字段顺序（不排序 key），使该哈希
    只在"工具定义真的变了"时变化。

    Args:
        tools: 当前帧可见工具的 OpenAI function schema 列表。

    Returns:
        16 位十六进制哈希摘要。
    """
    serialized = json.dumps(tools, ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def compute_system_core_hash(messages: list[dict[str, Any]]) -> str:
    """对开头连续 system 消息的内容做哈希，代表当前帧 system prompt 的指纹。

    Args:
        messages: 当前帧的完整消息列表。

    Returns:
        16 位十六进制哈希摘要；没有前导 system 消息时为空内容的哈希。
    """
    parts: list[str] = []
    for message in messages:
        if message.get("role") != "system":
            break
        parts.append(flatten_message_text(message.get("content")))
    digest = hashlib.sha256("\n".join(parts).encode())
    return digest.hexdigest()[:16]


def _git_output(project_root: Path, *args: str) -> str | None:
    """运行只读 git 子命令并返回 stdout；git 不可用/超时/非 git 仓库时返回 None。"""
    try:
        result = subprocess.run(  # noqa: S603 - 固定参数列表，无 shell、无用户输入拼接
            ["git", *args],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Repo fingerprint git command unavailable args=%s error=%s", args, exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _dependency_lock_hash(project_root: Path) -> str:
    """对工程内存在的清单/锁文件内容做哈希，文件全不存在时返回空串哈希。"""
    digest = hashlib.sha256()
    for name in _DEPENDENCY_LOCK_FILES:
        path = project_root / name
        try:
            data = path.read_bytes()
        except OSError:
            continue
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()[:16]


def _compute_repo_fingerprint_uncached(project_root: Path) -> str:
    """实际计算 repo 指纹：HEAD commit + 文件树 + 工作区脏状态 + 依赖锁的组合哈希。

    - `git rev-parse HEAD`：当前提交；
    - `git ls-files`：被跟踪文件树（file_tree_hash，感知增删文件/重命名）；
    - `git status --porcelain`：工作区未提交改动；
    - `_dependency_lock_hash`：常见清单/锁文件内容（dependency_lock_hash）。

    非 git 仓库或 git 不可用时退化为对 `project_root` 路径 + 依赖锁哈希，仍能
    区分"哪个工程"并感知锁文件变化，只是无法感知未被锁文件覆盖的文件级改动。
    """
    head = _git_output(project_root, "rev-parse", "HEAD")
    dependency_lock = _dependency_lock_hash(project_root)
    if head is None:
        fallback = hashlib.sha256(str(project_root).encode()).hexdigest()[:16]
        return f"no-git:{fallback}:{dependency_lock}"
    file_tree = _git_output(project_root, "ls-files") or ""
    file_tree_hash = hashlib.sha256(file_tree.encode()).hexdigest()[:16]
    status = _git_output(project_root, "status", "--porcelain") or ""
    digest = hashlib.sha256(f"{head}\n{file_tree_hash}\n{status}\n{dependency_lock}".encode())
    return digest.hexdigest()[:16]


class _RepoFingerprintCache:
    """带 TTL 的进程内 repo 指纹缓存，避免每次 LLM 调用都拉取 git 状态。"""

    def __init__(self, ttl_s: float = _REPO_FINGERPRINT_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._entries: dict[str, tuple[float, str]] = {}

    def get(self, project_root: Path) -> str:
        key = str(project_root)
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < self._ttl_s:
            return cached[1]
        fingerprint = _compute_repo_fingerprint_uncached(project_root)
        self._entries[key] = (now, fingerprint)
        return fingerprint


_default_fingerprint_cache = _RepoFingerprintCache()


def compute_repo_fingerprint(project_root: Path) -> str:
    """返回 `project_root` 当前状态的指纹，带 TTL 本地缓存（§16.1）。

    该函数会同步调用 `git`（最多等待 `_GIT_TIMEOUT_S` 秒），调用方需在线程池
    （如 `asyncio.to_thread`）里执行，避免阻塞事件循环。

    Args:
        project_root: 当前安全边界的工程根目录（`SecuritySettings.project_root`）。

    Returns:
        指纹字符串；同一 `project_root` 在 TTL 内复用缓存结果。
    """
    return _default_fingerprint_cache.get(project_root)


def compute_project_id(project_root: Path) -> str:
    """对工程根目录的绝对路径做哈希，作为稳定的工程标识（project_id）。

    用于项目级缓存：同一工程在不同会话间共享同一 `project_id`，使缓存键里
    "属于哪个工程"这一维度稳定可比。
    """
    return hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()[:16]


def compute_rag_fingerprint(rag_index_path: Path | None, query: str = "") -> str:
    """计算 deterministic(query + index_state + strategy_version) 指纹。

    RAG 索引重建后该指纹变化，使依赖检索结果的缓存段（L3）随之失效；只读
    元数据、不读取索引全文，避免大文件 I/O。

    Args:
        rag_index_path: 本地 RAG 索引文件路径；为 None 时视为无 RAG。

    Returns:
        指纹字符串，或 `no-rag`（无索引/不可读）。
    """
    if rag_index_path is None:
        logger.debug("RAG fingerprint skipped reason=no_index_path")
        return "no-rag"
    try:
        import json

        raw = rag_index_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("RAG fingerprint unavailable path=%s error=%s", rag_index_path, exc)
        return "no-rag"
    try:
        parsed = json.loads(raw)
        data = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        data = {}
    file_states = data.get("file_states", {}) if isinstance(data, dict) else {}
    if isinstance(file_states, dict) and file_states:
        state = "\n".join(
            f"{path}:{value.get('size', 0)}:{value.get('mtime_ns', 0)}"
            for path, value in sorted(file_states.items()) if isinstance(value, dict)
        )
    else:
        # Legacy indexes still use content rather than coarse filesystem metadata.
        state = raw
    from app.rag.hybrid import RETRIEVAL_STRATEGY_VERSION

    normalized_query = " ".join(query.strip().lower().split())
    index_state_hash = hashlib.sha256(
        f"schema:{data.get('schema_version', 'unknown')}\n{state}".encode()
    ).hexdigest()
    digest = hashlib.sha256(
        f"{normalized_query}\n{index_state_hash}\n{RETRIEVAL_STRATEGY_VERSION}".encode()
    )
    fingerprint = digest.hexdigest()[:16]
    logger.debug(
        "RAG fingerprint computed path=%s query_length=%d schema=%s strategy=%s fingerprint=%s",
        rag_index_path,
        len(query),
        data.get("schema_version", "unknown"),
        RETRIEVAL_STRATEGY_VERSION,
        fingerprint,
    )
    return fingerprint


def read_graph_versions(rag_index_path: Path | None) -> tuple[str, str]:
    """读取确定性的 scene/asset graph 版本；未构建固定返回 none。"""
    if rag_index_path is None:
        return "none", "none"
    import json

    values: list[str] = []
    for name in ("scene_graph.json", "asset_index.json"):
        try:
            data = json.loads((rag_index_path.parent / name).read_text(encoding="utf-8"))
            values.append(str(data.get("version", "none")))
        except (OSError, ValueError):
            values.append("none")
    logger.debug(
        "RAG graph versions read directory=%s scene=%s asset=%s",
        rag_index_path.parent,
        values[0],
        values[1],
    )
    return values[0], values[1]


def build_cache_key(
    *,
    system_core_hash: str,
    tool_schema_version: str,
    repo_fingerprint: str,
    project_id: str = "",
    rag_fingerprint: str = "",
    scene_graph_version: str = "none",
    asset_graph_version: str = "none",
) -> str:
    """组合各稳定性指纹为单一缓存键，仅供本服务内部判断前缀是否变化。

    Args:
        system_core_hash: 当前帧 system prompt 的内容哈希。
        tool_schema_version: 当前可见工具 schema 的哈希。
        repo_fingerprint: 当前工程根目录的状态指纹。
        project_id: 工程标识（见 `compute_project_id`）。
        rag_fingerprint: RAG 索引指纹（见 `compute_rag_fingerprint`）。

    Returns:
        各维度拼接后的 sha256 摘要；不会出现在发给 LLM 端点的请求体里。
    """
    material = ":".join(
        [system_core_hash, tool_schema_version, repo_fingerprint, project_id,
         scene_graph_version, asset_graph_version, rag_fingerprint]
    )
    return hashlib.sha256(material.encode()).hexdigest()
