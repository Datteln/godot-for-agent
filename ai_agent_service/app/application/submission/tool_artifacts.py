"""Front image enrichment and staged Map artifact storage."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.application.publication import SubmissionScope
from app.config import AppSettings
from app.orchestrator.map_artifacts import (
    MapArtifactLocator,
    MapArtifactStore,
    StagedMapArtifactTurn,
)
from app.orchestrator.map_workers import MAP_VALIDATION_TOOL_NAMES
from app.query.helpers import _json_char_size
from app.rag.asset_llm_client import AssetLLMClient, AssetLLMConfig
from app.security.settings import SecuritySettings
from app.sessions.store import SessionStore

logger = logging.getLogger(__name__)


class ToolArtifactService:
    """Owns artifact staging and optional visual enrichment."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SessionStore,
        available_tools: Callable[[], set[str]],
    ) -> None:
        self._settings = settings
        self._store = store
        self._available_tools = available_tools

    @property
    def available_tools(self) -> set[str]:
        return self._available_tools()

    def store_map_artifact(
        self,
        session_id: str,
        turn_id: str,
        tool_use_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        result: Any,
        publication_scope: SubmissionScope | None,
    ) -> MapArtifactLocator | None:
        """把大型地图工具结果加入当前事务的会话级聚合 turn。"""
        if tool_name not in {
            "describe_map_region",
            "compute_reachable_frontier",
            "query_spatial_index",
            "validate_map_region",
            "validate_layer_coverage",
            "validate_object_placements",
        }:
            return None
        if not isinstance(result, dict):
            return None
        if tool_name == "describe_map_region" and not (
            isinstance(result.get("cells"), list) or "atlas_summary" in result
        ):
            return None
        if tool_name == "query_spatial_index" and not isinstance(result.get("matches"), list):
            return None
        if tool_name in MAP_VALIDATION_TOOL_NAMES and _json_char_size(result) < 8_000:
            return None
        session = self._store.get_or_create(session_id, self.available_tools)
        store = MapArtifactStore(
            self._settings.project_root,
            session_id,
            session_epoch=session.session_epoch,
        )
        publication_buffer = publication_scope
        if publication_buffer is not None:
            publication_buffer.map_artifact_turn.add_entry(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                tool_args=tool_args,
                result=result,
            )
            entry = publication_buffer.map_artifact_turn.entries[tool_use_id]
            return store.locator(
                publication_buffer.turn_id,
                tool_use_id,
                str(entry.get("fingerprint", "")),
            )
        staged = StagedMapArtifactTurn(
            session_id=session_id,
            turn_id=turn_id,
            request_id=None,
            session_epoch=session.session_epoch,
        )
        staged.add_entry(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            tool_args=tool_args,
            result=result,
        )
        try:
            store.merge_turn(staged)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to write map artifact session=%s tool=%s turn=%s error=%s",
                session_id,
                tool_name,
                turn_id,
                exc,
            )
            return None
        entry = staged.entries[tool_use_id]
        return store.locator(
            turn_id,
            tool_use_id,
            str(entry.get("fingerprint", "")),
        )

    async def enrich_front_image(
        self,
        tool_name: str,
        result: dict[str, Any],
        security: SecuritySettings,
        tool_args: dict[str, Any],
    ) -> dict[str, Any]:
        """为前端读图类工具结果补充多模态语义描述。"""
        if tool_name not in {"read_image_metadata", "capture_viewport_screenshot"}:
            return result
        enriched = dict(result)
        client = AssetLLMClient(
            AssetLLMConfig(
                enabled=self._settings.asset_understanding_enabled,
                model=self._settings.asset_understanding_model,
                endpoint=self._settings.asset_understanding_endpoint,
                api_key=self._settings.asset_understanding_api_key.get_secret_value(),
                timeout_s=self._settings.asset_understanding_timeout_s,
                max_tokens=self._settings.asset_understanding_max_tokens,
                concurrency=1,
            )
        )
        semantic: dict[str, Any] = {
            "enabled": client.available,
            "model": self._settings.asset_understanding_model,
            "authority": "visual_only",
            "exact_fact_tools": ["describe_map_context", "describe_map_region"],
        }
        raw_question = tool_args.get("question") if tool_name == "read_image_metadata" else None
        question = raw_question.strip()[:2000] if isinstance(raw_question, str) else ""
        if question:
            semantic["question"] = question
        if not client.available:
            semantic["skipped"] = "asset_understanding_not_configured"
            enriched["semantic"] = semantic
            return enriched
        image_path = self._resolve_front_image_path(enriched, security)
        if image_path is None:
            semantic["skipped"] = "image_path_not_readable_by_service"
            enriched["semantic"] = semantic
            return enriched
        description = await asyncio.to_thread(
            client.describe,
            image_path,
            "image",
            question or None,
        )
        semantic["source_path"] = str(image_path)
        semantic["description"] = description
        semantic["answer"] = description
        enriched["semantic"] = semantic
        if description:
            enriched["semantic_description"] = description
        return enriched

    def _resolve_front_image_path(
        self, result: dict[str, Any], security: SecuritySettings
    ) -> Path | None:
        """把前端返回的 res/user 路径解析为服务端可读的本地图片路径。"""
        raw_path = str(result.get("path", "")).strip()
        if raw_path.startswith("res://"):
            rel = raw_path.removeprefix("res://").lstrip("/\\")
            return self._resolve_project_image_path(security.project_root / rel, security)
        if raw_path and not raw_path.startswith("user://") and not Path(raw_path).is_absolute():
            return self._resolve_project_image_path(security.project_root / raw_path, security)
        absolute = str(result.get("absolute_path", "")).strip()
        if raw_path.startswith("user://") and absolute:
            return self._resolve_existing_image_path(Path(absolute))
        return None

    def _resolve_project_image_path(
        self, candidate: Path, security: SecuritySettings
    ) -> Path | None:
        """确认项目内图片路径没有越过安全根目录且真实存在。"""
        try:
            resolved_root = security.project_root.resolve()
            resolved_candidate = candidate.resolve()
            resolved_candidate.relative_to(resolved_root)
        except (OSError, ValueError):
            return None
        return self._resolve_existing_image_path(resolved_candidate)

    def _resolve_existing_image_path(self, candidate: Path) -> Path | None:
        """确认图片候选路径存在且是普通文件。"""
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            return None
        return None
