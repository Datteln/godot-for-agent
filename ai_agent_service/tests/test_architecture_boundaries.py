"""Clean-cut architecture and removed-surface release guards."""

from __future__ import annotations

import ast
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SERVICE_ROOT.parent
APP_ROOT = SERVICE_ROOT / "app"
FRONTEND_ADDON = REPOSITORY_ROOT / "ai_agent_frontend" / "addons" / "ai_agent"


def _python_trees() -> list[tuple[Path, ast.AST]]:
    """Parse every runtime module so symbol checks are syntax-aware."""
    return [
        (path, ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path)))
        for path in APP_ROOT.rglob("*.py")
    ]


def test_deleted_runtime_facades_and_compatibility_symbols_cannot_return() -> None:
    """The integration cut has no old module, alias, converter, or driver symbol."""
    assert not (APP_ROOT / "orchestrator" / "agent.py").exists()
    assert not (APP_ROOT / "query" / "engine.py").exists()
    forbidden_classes = {"AgentApplication", "QueryEngine", "StepResult"}
    forbidden_functions = {"run_turn", "_read_legacy_entry", "adopt_legacy_artifact_epoch"}
    for path, tree in _python_trees():
        classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not classes & forbidden_classes, path
        assert not functions & forbidden_functions, path


def test_generic_turn_driver_has_no_map_implementation_dependency() -> None:
    """TurnDriver depends only on its protocol and canonical TurnOutcome."""
    path = APP_ROOT / "orchestrator" / "turn" / "driver.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(name.startswith("app.orchestrator.map") for name in imports)
    assert "map_task_state" not in source
    assert "pending_map" not in source
    for tool_name in ("edit_map", "validate_map_region", "delegate", "create_plan"):
        assert tool_name not in source
    assert "for transition_index in range(maximum)" in source
    assert "TurnDirective" in source


def test_turn_driver_exhaustively_handles_all_directive_variants() -> None:
    """Every retained TurnDirective variant has a reachable executor in drive()."""
    driver_source = (APP_ROOT / "orchestrator" / "turn" / "driver.py").read_text(
        encoding="utf-8"
    )
    # Each directive type must be referenced in the driver source.
    for directive_name in (
        "ContinueModel",
        "SuspendForFrontend",
        "PauseWorkflow",
        "CompleteTurn",
        "FailTurn",
    ):
        assert directive_name in driver_source, (
            f"TurnDriver does not reference directive {directive_name}"
        )
    assert "unapplied turn directive" in driver_source


def test_turn_core_does_not_import_map_turn_package() -> None:
    """Generic turn-core modules must not depend on Map turn handlers."""
    turn_dir = APP_ROOT / "orchestrator" / "turn"
    for py_file in turn_dir.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("app.orchestrator.map_turn"), (
                    f"{py_file.name} imports {module}"
                )
                assert not module.startswith("app.orchestrator.map_turn_pipeline"), (
                    f"{py_file.name} imports {module}"
                )


def test_map_turn_leaf_handlers_do_not_import_adapters() -> None:
    """Leaf handlers must not import policy or execution adapters."""
    map_turn_dir = APP_ROOT / "orchestrator" / "map_turn"
    if not map_turn_dir.exists():
        return
    leaf_modules = (
        "frame_lifecycle.py",
        "structured_completion.py",
        "planning.py",
        "delegation.py",
        "tool_arguments.py",
        "tool_guards.py",
        "tool_dispatch.py",
        "budgets.py",
        "events.py",
        "runtime.py",
    )
    for name in leaf_modules:
        path = map_turn_dir / name
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert ".policy" not in module, f"{name} imports {module}"
                assert ".execution" not in module, f"{name} imports {module}"


def test_map_turn_policy_adapter_within_size_budget() -> None:
    """MapTurnPolicy adapter must be ≤ 200 logical lines."""
    policy_path = APP_ROOT / "orchestrator" / "map_turn" / "policy.py"
    if not policy_path.exists():
        return
    source = policy_path.read_text(encoding="utf-8")
    logical_lines = [
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(logical_lines) <= 200, (
        f"MapTurnPolicy has {len(logical_lines)} logical lines, budget is 200"
    )


def test_event_transport_is_websocket_only_without_polling_compatibility() -> None:
    """Release runtime exposes snapshot recovery but no continuous event polling API."""
    route_source = (APP_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    schema_source = (APP_ROOT / "api" / "schemas.py").read_text(encoding="utf-8")
    frontend_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in FRONTEND_ADDON.rglob("*.gd")
        if path.name != "config_migrations.gd"
    )
    removed_settings_guard = (
        FRONTEND_ADDON / "config" / "config_migrations.gd"
    ).read_text(encoding="utf-8")
    assert '"/chat/socket"' in route_source
    assert '"/chat/snapshot"' in route_source
    assert "/chat/events" not in route_source
    assert "ChatEventsResponse" not in schema_source
    assert "ChatEventDTO" not in schema_source
    assert "_event_http" not in frontend_source
    assert "event_poll_interval" not in frontend_source
    assert "/chat/events" not in frontend_source
    assert "Removed polling setting is unsupported" in removed_settings_guard


def test_no_dual_writer_or_legacy_map_artifact_reader_is_reintroduced() -> None:
    """Current-schema workflow and canonical artifacts have one read/write path."""
    store_source = (APP_ROOT / "sessions" / "store.py").read_text(encoding="utf-8")
    artifact_source = (APP_ROOT / "orchestrator" / "map_artifacts.py").read_text(
        encoding="utf-8"
    )
    assert "workflow_event_tail" not in store_source
    assert "_read_legacy_entry" not in artifact_source
    assert "adopt_legacy_artifact_epoch" not in artifact_source
    assert "legacy_completed_response" not in store_source


def test_replaced_surfaces_have_no_rollback_feature_flag_or_sync_facade() -> None:
    """The new architecture is unconditional and exposes no old execution entry."""
    config_source = (APP_ROOT / "config.py").read_text(encoding="utf-8")
    assert not (APP_ROOT / "orchestrator" / "map_turn_pipeline.py").exists()
    assert not (APP_ROOT / "application" / "service.py").exists()
    assert "macro_v2_enforced" not in config_source


def test_chat_panel_uses_explicit_controllers_and_websocket_presentation() -> None:
    """Transport commands and render-queue ownership cannot drift back into ChatPanel."""
    panel = (FRONTEND_ADDON / "ui" / "chat_panel.gd").read_text(encoding="utf-8")
    controller_root = FRONTEND_ADDON / "controllers"
    for filename in (
        "submission_controller.gd",
        "tool_approval_controller.gd",
        "history_controller.gd",
        "recovery_controller.gd",
        "chat_streaming_controller.gd",
    ):
        assert (controller_root / filename).exists(), filename
        assert filename in panel
    assert "var _event_queue" not in panel
    assert "var _pending_calls" not in panel
    assert "_http_client.send_user_message" not in panel
    assert "_http_client.send_tool_results" not in panel
    assert "presentation awaits WebSocket event" in panel


def test_canonical_timeline_is_the_only_chat_item_rendering_authority() -> None:
    """可见聊天项只能经过 Projector、Store、Registry 与 Store 驱动的滚动器。"""
    panel = (FRONTEND_ADDON / "ui" / "chat_panel.gd").read_text(encoding="utf-8")
    scroller = (FRONTEND_ADDON / "ui" / "chat_virtual_scroller.gd").read_text(
        encoding="utf-8"
    )
    projector = (
        FRONTEND_ADDON / "timeline" / "chat_timeline_projector.gd"
    ).read_text(encoding="utf-8")
    registry = (
        FRONTEND_ADDON / "timeline" / "chat_item_renderer_registry.gd"
    ).read_text(encoding="utf-8")
    timeline_controller = (
        FRONTEND_ADDON / "controllers" / "chat_timeline_controller.gd"
    ).read_text(encoding="utf-8")
    state_reducer = (
        FRONTEND_ADDON / "state" / "session_turn_state_reducer.gd"
    ).read_text(encoding="utf-8")
    application_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (APP_ROOT / "application").rglob("*.py")
    )
    schema_source = (APP_ROOT / "api" / "schemas.py").read_text(encoding="utf-8")
    history_source = (APP_ROOT / "query" / "helpers.py").read_text(encoding="utf-8")

    assert not (FRONTEND_ADDON / "ui" / "chat_message_store.gd").exists()
    assert not (FRONTEND_ADDON / "ui" / "chat_node_factory.gd").exists()
    for forbidden in (
        "_queue_external_message",
        "external: true",
        "_message_fingerprint",
        "_rendered_assistant_keys",
        "_history_log_text",
        "_history_thought",
        "_history_code",
        "_history_front_tool_result",
    ):
        assert forbidden not in panel
        assert forbidden not in application_source
    assert "pseudo_events" not in schema_source
    assert "SessionHistoryItemDTO" not in schema_source
    assert "SessionHistoryItemDTO" not in history_source
    assert "legacy_stream" not in history_source
    assert "_history_text_fingerprint" not in history_source
    assert "_block_fingerprint" not in history_source
    assert "mutation_applied.connect(_on_store_mutation)" in scroller
    assert "create_item_node" in registry
    assert "Control.new" not in projector
    assert ".new()" not in projector
    for non_ui_owner in (timeline_controller, state_reducer):
        assert "Control.new" not in non_ui_owner
        assert "add_child" not in non_ui_owner


def test_application_modules_do_not_import_fastapi() -> None:
    """Application use cases and services must not depend on HTTP transport."""
    app_dir = APP_ROOT / "application"
    for py_file in app_dir.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("fastapi"), (
                    f"{py_file.name} imports FastAPI: {module}"
                )
                assert "starlette" not in module, (
                    f"{py_file.name} imports Starlette: {module}"
                )


def test_use_cases_do_not_forward_to_general_facade() -> None:
    """Route use cases depend on cohesive services, never a general facade."""
    use_cases_source = (APP_ROOT / "application" / "use_cases.py").read_text(
        encoding="utf-8"
    )
    assert "AgentApplication" not in use_cases_source
    assert "Protocol" in use_cases_source
    for port_name in (
        "SessionLifecyclePort",
        "MapTaskPort",
        "HistoryPort",
        "CompactionPort",
        "RecoveryPort",
    ):
        assert port_name in use_cases_source, (
            f"use_cases.py missing port: {port_name}"
        )
    for path in (APP_ROOT / "application").rglob("*.py"):
        assert "app.application.service" not in path.read_text(encoding="utf-8")


def test_submission_has_no_global_publication_or_decorative_use_case_path() -> None:
    """Submission state is explicit and old placeholder contracts stay deleted."""
    application_dir = APP_ROOT / "application"
    publication = (application_dir / "publication.py").read_text(encoding="utf-8")
    assert "ContextVar(" not in publication
    assert "_PUBLICATION_BUFFER =" not in publication
    assert not (application_dir / "contracts.py").exists()
    assert not (application_dir / "turn_execution.py").exists()
    assert "Placeholder" not in (
        application_dir / "submission" / "user_submission.py"
    ).read_text(encoding="utf-8")
    assert "Placeholder" not in (
        application_dir / "submission" / "tool_result_submission.py"
    ).read_text(encoding="utf-8")


def test_application_use_case_module_size_budgets() -> None:
    """Application use-case modules must stay within declared size budgets."""
    use_cases_path = APP_ROOT / "application" / "use_cases.py"
    use_cases_source = use_cases_path.read_text(encoding="utf-8")
    logical_lines = [
        line for line in use_cases_source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(logical_lines) <= 300, (
        f"use_cases.py has {len(logical_lines)} logical lines, budget is 300"
    )
    # response_mapping.py should be small
    mapping_path = APP_ROOT / "application" / "response_mapping.py"
    if mapping_path.exists():
        mapping_source = mapping_path.read_text(encoding="utf-8")
        mapping_lines = [
            line for line in mapping_source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(mapping_lines) <= 100, (
            f"response_mapping.py has {len(mapping_lines)} logical lines, budget is 100"
        )
    for path in (APP_ROOT / "application").rglob("*.py"):
        logical_lines = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(logical_lines) <= 700, (
            f"{path.relative_to(APP_ROOT)} has {len(logical_lines)} logical lines, budget is 700"
        )


def test_map_turn_handler_module_size_budgets() -> None:
    """Each Map turn handler module must stay within the declared size budget."""
    map_turn_dir = APP_ROOT / "orchestrator" / "map_turn"
    if not map_turn_dir.exists():
        return
    for path in map_turn_dir.glob("*.py"):
        budget = 200 if path.name == "policy.py" else 700
        source = path.read_text(encoding="utf-8")
        logical_lines = [
            line for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(logical_lines) <= budget, (
            f"{path.name} has {len(logical_lines)} logical lines, budget is {budget}"
        )
