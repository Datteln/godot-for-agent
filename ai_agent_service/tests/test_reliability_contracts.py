from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.events.websocket_protocol import EventBatchMessage, parse_client_message
from app.orchestrator.turn.contracts import TurnCommand
from app.verify.contracts import UnsupportedVerifySchemaError, VerifyOutcome
from app.workflow.contracts import (
    WorkflowEvent,
    WorkflowManifest,
    WorkflowSegment,
    WorkflowSnapshot,
)


class TurnContractTests(unittest.TestCase):
    def test_command_accepts_exactly_one_input_kind(self) -> None:
        command = TurnCommand(
            session_id="s1",
            session_epoch="e1",
            request_id="r1",
            user_text="hello",
        )
        self.assertEqual(command.user_text, "hello")

        with self.assertRaisesRegex(ValueError, "exactly one"):
            TurnCommand(
                session_id="s1",
                session_epoch="e1",
                request_id="r1",
            )


class VerifyContractTests(unittest.TestCase):
    def test_boolean_projection_is_rejected(self) -> None:
        with self.assertRaisesRegex(UnsupportedVerifySchemaError, "unsupported"):
            VerifyOutcome.from_payload(
                {
                    "passed": True,
                    "issues": [],
                    "summary": "legacy",
                }
            )

    def test_canonical_passed_outcome_has_no_boolean_projection(self) -> None:
        outcome = VerifyOutcome.from_payload(
            {
                "schema_version": 1,
                "status": "passed",
                "phase": "semantic",
                "reason_code": "verified",
                "summary": "verified",
                "issues": [],
                "attempt": 1,
                "max_attempts": 2,
                "retryable": False,
                "recovery_actions": [],
            }
        )
        self.assertNotIn("passed", outcome.to_payload())


class WorkflowContractTests(unittest.TestCase):
    def test_segment_snapshot_and_manifest_are_content_addressed(self) -> None:
        event = WorkflowEvent(1, "progress_recorded", "res://map.tscn", 1, {"count": 1})
        segment = WorkflowSegment.create(
            session_epoch="e1",
            lineage="l1",
            predecessor_digest=None,
            events=(event,),
        )
        snapshot = WorkflowSnapshot.create(
            session_epoch="e1",
            lineage="l1",
            snapshot_seq=0,
            state={"stage": "read"},
        )
        manifest = WorkflowManifest.create(
            session_epoch="e1",
            lineage="l1",
            high_water_seq=1,
            snapshot_digest=snapshot.digest,
            segment_digests=(segment.digest,),
            generation=1,
        )

        self.assertEqual(len(segment.digest), 64)
        self.assertEqual(manifest.high_water_seq, 1)


class WebSocketContractTests(unittest.TestCase):
    def test_resume_is_discriminated(self) -> None:
        message = parse_client_message(
            {
                "type": "resume",
                "protocol_version": 1,
                "session_id": "s1",
                "session_epoch": "e1",
                "after_seq": 0,
            }
        )
        self.assertEqual(message.type, "resume")

    def test_batch_rejects_sequence_gap(self) -> None:
        with self.assertRaises(ValidationError):
            EventBatchMessage.model_validate(
                {
                    "session_epoch": "e1",
                    "first_seq": 1,
                    "last_seq": 3,
                    "encoded_bytes": 100,
                    "events": [
                        {
                            "seq": 1,
                            "session_id": "s1",
                            "session_epoch": "e1",
                            "type": "one",
                        },
                        {
                            "seq": 3,
                            "session_id": "s1",
                            "session_epoch": "e1",
                            "type": "three",
                        },
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
