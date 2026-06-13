"""OutputStyle data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

OutputStyleSource = Literal["bundled", "user", "project", "plugin"]


@dataclass(frozen=True)
class OutputStyle:
    """OutputStyle is a prompt asset, not an authorization source."""

    qualified_name: str
    name: str
    source: OutputStyleSource
    description: str
    body: str
    file_path: Path
    enabled: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OutputStyleSummary:
    """API-safe OutputStyle summary."""

    qualified_name: str
    name: str
    source: OutputStyleSource
    description: str
    enabled: bool
    warnings: list[str]
