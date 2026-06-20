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


class CommandErrorStatusTests(unittest.TestCase):
    """协议层错误应返回恰当的 HTTP 状态码，同时保留结构化 body（ok/text）。"""

    def _client(self, root: Path) -> TestClient:
        app = create_app(
            AppSettings(project_root=root, rag_auto_build_enabled=False),
            token="test-token",
        )
        return TestClient(app)

    def test_unknown_command_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                resp = client.post(
                    "/commands/does_not_exist",
                    headers={"Authorization": "Bearer test-token"},
                    json={"session_id": "s1", "args": {}},
                )
                self.assertEqual(resp.status_code, 404)
                self.assertFalse(resp.json()["ok"])
                self.assertIn("未知命令", resp.json()["text"])

    def test_invalid_effort_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                resp = client.post(
                    "/commands/set_effort",
                    headers={"Authorization": "Bearer test-token"},
                    json={"session_id": "s1", "args": {"effort": "bogus"}},
                )
                self.assertEqual(resp.status_code, 400)
                self.assertFalse(resp.json()["ok"])

    def test_compact_missing_session_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                resp = client.post(
                    "/commands/compact",
                    headers={"Authorization": "Bearer test-token"},
                    json={"args": {}},
                )
                self.assertEqual(resp.status_code, 400)

    def test_memory_save_missing_text_returns_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                resp = client.post(
                    "/memory",
                    headers={"Authorization": "Bearer test-token"},
                    json={"action": "save"},
                )
                self.assertEqual(resp.status_code, 400)
                self.assertFalse(resp.json()["ok"])

    def test_memory_delete_unknown_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                resp = client.post(
                    "/memory",
                    headers={"Authorization": "Bearer test-token"},
                    json={"action": "delete", "id": "nope"},
                )
                self.assertEqual(resp.status_code, 404)
                self.assertFalse(resp.json()["ok"])

    def test_successful_memory_save_still_200(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                resp = client.post(
                    "/memory",
                    headers={"Authorization": "Bearer test-token"},
                    json={"action": "save", "text": "hello"},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertTrue(resp.json()["ok"])


if __name__ == "__main__":
    unittest.main()
