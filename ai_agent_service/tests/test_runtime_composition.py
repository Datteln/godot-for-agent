"""Composition-root import and factory lifecycle regression tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def test_importing_asgi_factory_constructs_no_runtime_objects(tmp_path: Path) -> None:
    """Factory import must not create an app, register tools, stores, or watchers."""
    script = (
        "import app.main; "
        "from app.tools.registry import REGISTRY; "
        "assert not hasattr(app.main, 'app'); "
        "assert len(REGISTRY) == 0; "
        "assert not list(__import__('pathlib').Path.cwd().iterdir())"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SERVICE_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_managed_cli_and_external_asgi_use_explicit_factory_symbols() -> None:
    """One module exposes the managed entry and factory without a global app."""
    from app import main as entrypoint

    assert callable(entrypoint.main)
    assert callable(entrypoint.create_app)
    assert not hasattr(entrypoint, "app")
