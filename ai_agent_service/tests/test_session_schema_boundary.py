"""Session v10 干净数据边界与工作流协调恢复测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.api.schemas import ChatRequest
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import AssistantTurn, LLMProvider
from app.orchestrator.map_workflow import (
    dispatch_map_workflow_event,
    make_map_workflow_event,
)
from app.sessions.store import SessionStore, session_to_dict
from app.storage.atomic import atomic_write_json
from tests.application_test_support import build_test_application


class _CountingProvider(LLMProvider):
    """记录是否越过 Session schema 边界调用了模型。"""

    def __init__(self) -> None:
        """初始化零调用计数。"""
        self.calls = 0

    @property
    def supports_tool_calling(self) -> bool:
        """声明支持工具协议。"""
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        """声明不支持提示缓存。"""
        return False

    async def chat(self, *_args: Any, **_kwargs: Any) -> AssistantTurn:
        """记录调用并返回固定内容。"""
        self.calls += 1
        return AssistantTurn(
            raw_message={"role": "assistant", "content": "unexpected"},
            content="unexpected",
            model="test",
        )


def _session_path(storage_dir: Path, session_id: str) -> Path:
    """按正式无碰撞规则计算测试 Session 文件路径。"""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return storage_dir / f"{digest}.json"


class SessionSchemaBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """验证旧 Session 被拒绝且当前 Session 只引用 manifest。"""

    async def test_legacy_session_is_rejected_unchanged_before_provider(self) -> None:
        """旧嵌入式工作流 Session 不迁移、不写回、也不触发模型。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "sessions"
            store = SessionStore(storage, project_root=root)
            epoch = store.current_epoch("legacy-session")
            legacy = {
                "schema_version": 9,
                "session_id": "legacy-session",
                "session_epoch": epoch,
                "map_task_state": {
                    "workflow_events": [{"event_type": "legacy"}],
                },
                "completed_tool_turn_cache": {
                    "turn-1": {"fingerprint": "legacy"},
                },
            }
            path = _session_path(storage, "legacy-session")
            atomic_write_json(path, legacy)
            before = path.read_bytes()
            provider = _CountingProvider()
            rig = build_test_application(
                settings=AppSettings(
                    llm_base_url="http://localhost",
                    project_root=root,
                    rag_auto_build_enabled=False,
                ),
                session_store=store,
                llm=provider,
                event_store=EventStore(),
            )

            response = await rig.execute(
                ChatRequest(session_id="legacy-session", user_message="continue")
            )

            self.assertEqual(response.type, "error")
            self.assertEqual(response.error_code, "unsupported_session_schema")
            self.assertEqual(response.next_action, {"action": "create_new_session"})
            self.assertEqual(provider.calls, 0)
            self.assertEqual(path.read_bytes(), before)

    async def test_current_session_document_has_only_manifest_reference(self) -> None:
        """当前 Session JSON 不再嵌入 MapTaskState 或事件历史尾。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "sessions"
            store = SessionStore(storage, project_root=root)
            session = store.get_or_create("current-session", set())
            dispatch_map_workflow_event(
                session.map_task_state,
                make_map_workflow_event(
                    session.map_task_state,
                    "progress_recorded",
                    "Map/Ground",
                    1,
                    {"category": "write", "count": 1},
                ),
            )
            store.save(session)

            payload = json.loads(
                _session_path(storage, "current-session").read_text(encoding="utf-8")
            )
            restarted = SessionStore(storage, project_root=root).get_or_create(
                "current-session", set()
            )

            self.assertNotIn("map_task_state", payload)
            self.assertNotIn("completed_tool_turn_cache", payload)
            self.assertEqual(set(payload["workflow"]), {
                "schema_epoch",
                "lineage",
                "manifest_digest",
                "generation",
            })
            self.assertEqual(restarted.map_task_state.workflow_high_water_seq, 1)

    async def test_restart_finishes_manifest_switch_selected_by_session(self) -> None:
        """Session 写入后中断的 manifest 切换可按提交摘要幂等完成。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / "sessions"
            store = SessionStore(storage, project_root=root)
            session = store.get_or_create("recover-session", set())
            store.save(session)
            dispatch_map_workflow_event(
                session.map_task_state,
                make_map_workflow_event(
                    session.map_task_state,
                    "progress_recorded",
                    "Map/Ground",
                    2,
                    {"category": "prepared", "count": 1},
                ),
            )
            workflow_store = store._workflow_store(  # noqa: SLF001 - crash-boundary test
                session.session_id,
                session.session_epoch,
            )
            prepared = workflow_store.prepare(
                session.map_task_state,
                lineage=session.workflow_lineage,
                commit_id="interrupted-commit",
            )
            session.workflow_manifest_digest = prepared.manifest.digest
            session.workflow_manifest_generation = prepared.manifest.generation
            atomic_write_json(
                _session_path(storage, session.session_id),
                session_to_dict(session),
            )

            restored = SessionStore(storage, project_root=root).get_or_create(
                "recover-session", set()
            )

            self.assertEqual(restored.map_task_state.workflow_high_water_seq, 1)
            current = workflow_store.current_manifest()
            self.assertEqual(current.digest, prepared.manifest.digest)
            self.assertFalse(prepared.prepared_manifest_path.exists())


if __name__ == "__main__":
    unittest.main()
