"""Focused tests for explicit application transaction services."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.application.progress import TurnProgressRegistry
from app.application.publication import PreviewLifecycle, SubmissionScope
from app.application.session_uow import SessionUnitOfWork
from app.sessions.store import SessionStore


class SubmissionScopeTests(unittest.TestCase):
    def test_buffer_requires_explicit_scope_and_preserves_identity(self) -> None:
        scope = SubmissionScope(
            session=object(),
            request_id="r1",
            turn_id="t1",
            map_artifact_turn=None,
        )

        scope.buffer_event("s1", "agent_step", {"loop": 1})

        self.assertEqual(scope.buffered_events(), [("s1", "agent_step", {"loop": 1})])
        self.assertFalse(scope.resolved)

    def test_discard_is_idempotent_and_rejects_late_buffering(self) -> None:
        scope = SubmissionScope(object(), "r1", "t1", None)
        scope.buffer_event("s1", "agent_step", {"loop": 1})

        scope.discard()
        scope.discard()

        self.assertTrue(scope.resolved)
        self.assertEqual(scope.buffered_events(), [])
        with self.assertRaisesRegex(RuntimeError, "already resolved"):
            scope.buffer_event("s1", "agent_step", {})

    def test_preview_lifecycle_has_stable_keys(self) -> None:
        preview = PreviewLifecycle()
        preview.add("p1", {"status": "pending"})
        preview.add("p1", {"status": "replacement"})
        preview.mark_resolved()

        self.assertEqual(preview.items, {"p1": {"status": "pending"}})
        self.assertTrue(preview.resolved)


class TurnProgressRegistryTests(unittest.TestCase):
    def test_owned_updates_and_removal_do_not_clobber_new_request(self) -> None:
        registry = TurnProgressRegistry()
        registry.upsert_owned(
            "s1",
            owner_id=1,
            request_id="r1",
            turn_id=None,
            phase="queued",
        )
        registry.upsert_owned(
            "s1",
            owner_id=2,
            request_id="r2",
            turn_id="t2",
            phase="model",
        )

        registry.remove_owned("s1", 1)
        snapshot = registry.heartbeat_snapshot("s1")

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["request_id"], "r2")
        self.assertEqual(snapshot["heartbeat_seq"], 1)


class SessionUnitOfWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_working_set_isolates_tool_result_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "sessions", project_root=root)
            session = store.get_or_create("s1", set())
            uow = SessionUnitOfWork(store)

            working_set = uow.working_set(session, isolate=True)
            working_set.session.effort = "high"

            self.assertNotEqual(working_set.snapshot.effort, "high")
            self.assertNotEqual(session.effort, "high")

    async def test_restore_replaces_failed_in_memory_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "sessions", project_root=root)
            session = store.get_or_create("s1", set())
            uow = SessionUnitOfWork(store)
            snapshot = uow.working_set(session, isolate=False).snapshot
            session.effort = "high"

            uow.restore("s1", snapshot)

            restored = store.get_or_create("s1", set())
            self.assertEqual(restored.effort, snapshot.effort)

    async def test_serialize_uses_store_session_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "sessions", project_root=root)
            uow = SessionUnitOfWork(store)

            async with uow.serialize("s1"):
                self.assertTrue(store.lock_for("s1").locked())


if __name__ == "__main__":
    unittest.main()
