from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routes import _integer_argument
from app.config import AppSettings
from app.main import create_app


class CommandArgumentTests(unittest.TestCase):
    def test_integral_json_number_is_accepted(self) -> None:
        self.assertEqual(_integer_argument(4000.0), 4000)
        self.assertEqual(_integer_argument(12), 12)

    def test_fraction_boolean_and_string_are_rejected(self) -> None:
        self.assertIsNone(_integer_argument(1.5))
        self.assertIsNone(_integer_argument(True))
        self.assertIsNone(_integer_argument("4000"))

    def test_frontend_formats_structured_command_results_as_text(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        panel = (
            repo / "ai_agent_frontend/addons/ai_agent/ui/chat_panel.gd"
        ).read_text(encoding="utf-8")
        self.assertIn("func _format_command_response", panel)
        self.assertIn("func _format_rebuild_index_result", panel)
        self.assertIn("func _format_compact_result", panel)
        self.assertNotIn(
            'command_text += "\\n\\n" + JSON.stringify(command_result', panel
        )

    def test_all_registered_commands_run_successfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text("func jump():\n    pass\n", encoding="utf-8")
            app = create_app(
                AppSettings(project_root=root, rag_auto_build_enabled=False),
                token="test-token",
            )
            headers = {"Authorization": "Bearer test-token"}
            payloads = {
                "doctor": {},
                "rebuild_index": {
                    "include": "**/*",
                    "incremental": True,
                    "max_files": 4000.0,
                },
                "compact": {"keep_recent": 12.0},
                "set_effort": {"effort": "standard"},
                "set_output_style": {"output_style": "default"},
                "refresh_extensions": {},
            }
            with TestClient(app) as client:
                for name, args in payloads.items():
                    response = client.post(
                        f"/commands/{name}",
                        headers=headers,
                        json={"session_id": "command-test", "args": args},
                    )
                    self.assertEqual(response.status_code, 200, name)
                    self.assertTrue(response.json()["ok"], (name, response.json()))


if __name__ == "__main__":
    unittest.main()
