"""OutputStyle discovery and loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import AppSettings
from app.output_styles.types import OutputStyle, OutputStyleSource, OutputStyleSummary


def _bundled_dir() -> Path:
    return Path(__file__).parent / "bundled"


def _parse_scalar(value: str) -> str:
    return value.strip().strip("\"'")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    meta: dict[str, Any] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        meta[key.strip()] = _parse_scalar(raw)
    return meta, "\n".join(lines[index:]).strip()


def _source_dirs(settings: AppSettings) -> list[tuple[OutputStyleSource, Path, bool]]:
    return [
        ("bundled", _bundled_dir(), True),
        ("project", settings.resolved_output_styles_dir(), settings.trusted_project),
    ]


class OutputStyleCatalog:
    """OutputStyle registry."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._styles: dict[str, OutputStyle] = {}
        self.refresh()

    def refresh(self) -> None:
        styles: dict[str, OutputStyle] = {}
        for source, root, enabled in _source_dirs(self._settings):
            if not root.exists():
                continue
            for path in sorted(root.glob("*.md")):
                style = self._load_file(source, path, enabled)
                styles[style.qualified_name] = style
        self._styles = styles

    def get(self, name: str | None) -> OutputStyle | None:
        if not name:
            name = "default"
        if name in self._styles:
            return self._styles[name]
        matches = [style for style in self._styles.values() if style.name == name]
        if len(matches) == 1:
            return matches[0]
        bundled = self._styles.get(f"bundled:{name}")
        return bundled

    def summaries(self) -> list[OutputStyleSummary]:
        return [
            OutputStyleSummary(
                qualified_name=style.qualified_name,
                name=style.name,
                source=style.source,
                description=style.description,
                enabled=style.enabled,
                warnings=style.warnings,
            )
            for style in sorted(self._styles.values(), key=lambda item: item.qualified_name)
        ]

    def _load_file(self, source: OutputStyleSource, path: Path, enabled: bool) -> OutputStyle:
        meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        name = str(meta.get("name") or path.stem)
        description = str(meta.get("description") or "")
        warnings: list[str] = []
        if not description:
            warnings.append("缺少 description")
        if source == "project" and not enabled:
            warnings.append("项目级 OutputStyle 需要 trusted_project=true 后才启用")
        return OutputStyle(
            qualified_name=f"{source}:{name}",
            name=name,
            source=source,
            description=description,
            body=body,
            file_path=path,
            enabled=enabled,
            warnings=warnings,
        )
