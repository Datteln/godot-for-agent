"""Architecture and phase-routing tests for the canonical Map turn package."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.orchestrator.map_turn.budgets import (
    _uses_persistent_map_budget,
)
from app.orchestrator.map_turn.execution import MapTransitionEngine
from app.orchestrator.map_turn.runtime import MapModelStep, MapToolStep
from app.orchestrator.turn.contracts import ContinueModel, FinalTurnOutcome

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "app" / "orchestrator" / "map_turn"


class MapTransitionEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_outcome_is_converted_to_terminal_directive(self) -> None:
        context = object()
        engine = MapTransitionEngine(context)  # type: ignore[arg-type]
        with patch(
            "app.orchestrator.map_turn.execution.run_model_cycle",
            AsyncMock(return_value=FinalTurnOutcome(text="done")),
        ):
            directive = await engine.transition(0)

        self.assertEqual(directive.kind, "complete_turn")
        self.assertEqual(directive.text, "done")

    async def test_continue_skips_response_and_tool_phases(self) -> None:
        context = object()
        engine = MapTransitionEngine(context)  # type: ignore[arg-type]
        route = AsyncMock()
        execute = AsyncMock()
        with (
            patch(
                "app.orchestrator.map_turn.execution.run_model_cycle",
                AsyncMock(return_value=ContinueModel(reason="retry")),
            ),
            patch("app.orchestrator.map_turn.execution.route_model_response", route),
            patch("app.orchestrator.map_turn.execution.execute_tool_cycle", execute),
        ):
            directive = await engine.transition(1)

        self.assertEqual(directive, ContinueModel(reason="retry"))
        route.assert_not_awaited()
        execute.assert_not_awaited()

    async def test_model_and_tool_stage_are_routed_in_order(self) -> None:
        context = object()
        engine = MapTransitionEngine(context)  # type: ignore[arg-type]
        model_step = object()
        tool_step = object()
        # Runtime isinstance checks use the real DTO classes; construct sentinels
        # without invoking their field validators.
        model_step = object.__new__(MapModelStep)
        tool_step = object.__new__(MapToolStep)
        run_model = AsyncMock(return_value=model_step)
        route = AsyncMock(return_value=tool_step)
        execute = AsyncMock(return_value=FinalTurnOutcome(text="tool done"))
        with (
            patch("app.orchestrator.map_turn.execution.run_model_cycle", run_model),
            patch("app.orchestrator.map_turn.execution.route_model_response", route),
            patch("app.orchestrator.map_turn.execution.execute_tool_cycle", execute),
        ):
            directive = await engine.transition(2)

        route.assert_awaited_once_with(context, model_step)
        execute.assert_awaited_once_with(context, tool_step)
        self.assertEqual(directive.kind, "complete_turn")


class MapTurnPackageStructureTests(unittest.TestCase):
    REQUIRED_MODULES = {
        "policy.py",
        "execution.py",
        "runtime.py",
        "model_cycle.py",
        "response_routing.py",
        "tool_cycle.py",
        "budgets.py",
        "events.py",
    }

    def test_required_modules_exist(self) -> None:
        existing = {path.name for path in PACKAGE_ROOT.glob("*.py")}
        self.assertTrue(self.REQUIRED_MODULES <= existing)

    def test_deleted_monolith_does_not_exist(self) -> None:
        monolith = PACKAGE_ROOT.parent / "map_turn_pipeline.py"
        self.assertFalse(monolith.exists())

    def test_policy_is_small_and_does_not_own_model_or_tool_calls(self) -> None:
        source = (PACKAGE_ROOT / "policy.py").read_text(encoding="utf-8")
        logical = [line for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertLessEqual(len(logical), 200)
        self.assertNotIn("invoke_model", source)
        self.assertNotIn("execute_server_tools", source)

    def test_leaf_modules_do_not_depend_on_policy_or_execution(self) -> None:
        for name in ("budgets.py", "events.py", "runtime.py"):
            tree = ast.parse((PACKAGE_ROOT / name).read_text(encoding="utf-8"))
            imports = {
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            self.assertFalse(any(module.endswith((".policy", ".execution")) for module in imports))


class MapBudgetTests(unittest.TestCase):
    def test_persistent_budget_uses_explicit_pipeline_kind(self) -> None:
        map_frame = type(
            "Frame",
            (),
            {"agent": type("Agent", (), {"pipeline_kind": "map"})()},
        )()
        chat_frame = type(
            "Frame",
            (),
            {"agent": type("Agent", (), {"pipeline_kind": "chat"})()},
        )()

        self.assertTrue(_uses_persistent_map_budget(map_frame))
        self.assertFalse(_uses_persistent_map_budget(chat_frame))


if __name__ == "__main__":
    unittest.main()
