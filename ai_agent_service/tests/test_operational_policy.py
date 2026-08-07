from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.config import AppSettings


class OperationalPolicyTests(unittest.TestCase):
    """Validated policy must fail before runtime services are composed."""

    def test_effective_policy_contains_no_secrets_or_endpoints(self) -> None:
        policy = AppSettings().effective_operational_policy()

        self.assertEqual(policy["provider_max_attempts"], 3)
        self.assertEqual(policy["websocket_batch_event_limit"], 64)
        self.assertFalse(any("key" in name or "url" in name for name in policy))

    def test_unacknowledged_bound_cannot_be_smaller_than_batch(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unacked_event_limit"):
            AppSettings(
                websocket_batch_event_limit=64,
                websocket_unacked_event_limit=32,
            )

    def test_reconnect_cap_cannot_be_smaller_than_initial_delay(self) -> None:
        with self.assertRaisesRegex(ValidationError, "reconnect_max_s"):
            AppSettings(
                websocket_reconnect_initial_s=2.0,
                websocket_reconnect_max_s=1.0,
            )

    def test_removed_polling_environment_is_rejected(self) -> None:
        environment = {**os.environ, "AI_AGENT_EVENT_POLL_INTERVAL_SEC": "1.0"}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValidationError, "removed polling settings"):
                AppSettings()


if __name__ == "__main__":
    unittest.main()
