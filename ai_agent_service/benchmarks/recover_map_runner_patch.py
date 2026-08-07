"""分块生成 runner 源码恢复补丁，避免工具输出截断。"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def _replacement(repo: Path) -> list[str]:
    """从已跟踪前身与当前协议差异构造丢失的 runner 片段。"""
    head = subprocess.check_output(
        ["git", "show", "HEAD:ai_agent_service/app/orchestrator/agent.py"],
        cwd=repo.parent,
        text=True,
        encoding="utf-8",
    ).splitlines()
    lines = [
        "                            cache_decision.breakpoints",
        "                            if cache_decision is not None and cache_decision.enabled",
        "                            else None",
        "                        ),",
        "                        response_contract=response_contract,",
        "                    ),",
        "                )",
        "            except LLMError as exc:",
        "                logger.warning(",
        '                    "Agent LLM step failed session=%s frame=%s error_code=%s "',
        '                    "model=%s wire_attempts=%d",',
        "                    session.session_id,",
        "                    frame.id,",
        "                    exc.error_code,",
        "                    exc.model or resolved_model,",
        "                    exc.wire_attempt_count,",
        "                )",
        "                if (",
        "                    session.map_request_scope.activates_map_gate",
        "                    and session.map_request_scope.map_task_id == session.map_task_state.task_id",
        '                    and session.map_task_state.status == "running"',
        "                ):",
        "                    session.map_task_state.make_checkpoint(",
        "                        exc.error_code,",
        '                        pause_kind="provider_exhausted",',
        "                    )",
        "                    return ErrorTurnOutcome(",
        "                        text=(",
        "                            str(exc)",
        '                            if exc.error_code == "partial_stream_interrupted"',
        "                            else map_pause_message(session.map_task_state)",
        "                        ),",
        "                        error_code=exc.error_code,",
        "                    )",
        "                return ErrorTurnOutcome(text=str(exc), error_code=exc.error_code)",
        "",
    ]
    lines.extend(("    " + line) if line else "" for line in head[5003:5366])
    return lines


def render_chunk(repo: Path, index: int, chunk_size: int) -> str:
    """输出一个以稳定占位符衔接的 apply_patch 分块。"""
    runner = repo / "app/orchestrator/map_turn/runner.py"
    all_lines = _replacement(repo)
    chunks = [
        all_lines[offset : offset + chunk_size]
        for offset in range(0, len(all_lines), chunk_size)
    ]
    if index < 0 or index >= len(chunks):
        raise ValueError(f"chunk index out of range: {index}/{len(chunks)}")
    source_lines = runner.read_text(encoding="utf-8").splitlines()
    if index == 0:
        old = next(line for line in source_lines if "tokens truncated" in line)
    else:
        old = f"# MAP_RUNNER_RECOVERY_{index}"
    replacement = list(chunks[index])
    if index + 1 < len(chunks):
        replacement.append(f"# MAP_RUNNER_RECOVERY_{index + 1}")
    patch = [
        "*** Begin Patch",
        f"*** Update File: {runner.resolve()}",
        "@@",
        f"-{old}",
    ]
    patch.extend(f"+{line}" for line in replacement)
    patch.append("*** End Patch")
    return "\n".join(patch)


def main() -> int:
    """解析分块参数并输出恢复补丁。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=int)
    parser.add_argument("--chunk-size", type=int, default=100)
    args = parser.parse_args()
    print(render_chunk(Path.cwd(), args.index, args.chunk_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
