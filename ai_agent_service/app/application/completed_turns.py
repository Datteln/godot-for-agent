"""Durable completed-turn identity ledger with a bounded response hot cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.storage.atomic import atomic_write_json

if TYPE_CHECKING:
    from app.sessions.store import Session


class CompletedTurnConflictError(ValueError):
    """The same turn id was submitted with a different canonical fingerprint."""


class CompletedTurnIntegrityError(ValueError):
    """A durable response locator cannot reconstruct its committed outcome."""


@dataclass(frozen=True, slots=True)
class CompletedTurnResolution:
    response: dict[str, Any]
    source: str


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    """将映射编码为稳定的 UTF-8 JSON 字节。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_clone(value: Mapping[str, Any]) -> dict[str, Any]:
    """通过规范 JSON 创建不共享嵌套引用的响应副本。"""
    cloned = json.loads(_canonical_bytes(value))
    if not isinstance(cloned, dict):
        raise TypeError("completed-turn response must be a JSON object")
    return cloned


class CompletedTurnLedger:
    """Persist compact authority in Session and reconstruct evicted responses by locator."""

    def __init__(self, project_root: Path, hot_cache_size: int) -> None:
        """创建限定在项目内部的完成回合账本访问器。"""
        if hot_cache_size < 1:
            raise ValueError("completed-turn hot cache size must be positive")
        self._root = project_root.resolve() / ".ai_agent_service" / "completed_turns"
        self._hot_cache_size = hot_cache_size

    def resolve(
        self,
        session: Session,
        *,
        turn_id: str,
        fingerprint: str,
    ) -> CompletedTurnResolution | None:
        """解析相同提交的原始响应，冲突或损坏时拒绝继续。"""
        raw_entry = session.completed_turn_ledger.get(turn_id)
        if not isinstance(raw_entry, dict):
            return None
        if raw_entry.get("fingerprint") != fingerprint:
            raise CompletedTurnConflictError(
                "same turn_id was already committed with a different fingerprint"
            )
        expected_digest = str(raw_entry.get("commit_digest", ""))
        outcome_kind = str(raw_entry.get("outcome_kind", ""))
        if not expected_digest or not outcome_kind:
            raise CompletedTurnIntegrityError("completed-turn ledger entry is incomplete")
        cached = session.completed_response_hot_cache.get(turn_id)
        if isinstance(cached, dict):
            response = _canonical_clone(cached)
            actual_digest = self._commit_digest(turn_id, fingerprint, response)
            if actual_digest != expected_digest:
                raise CompletedTurnIntegrityError(
                    "completed-turn hot-cache commit digest mismatch"
                )
            if str(response.get("type", "error")) != outcome_kind:
                raise CompletedTurnIntegrityError(
                    "completed-turn hot-cache outcome kind mismatch"
                )
            return CompletedTurnResolution(response, "hot_cache")
        locator = str(raw_entry.get("response_locator", ""))
        payload = self._load_locator(locator)
        if (
            payload.get("session_epoch") != session.session_epoch
            or payload.get("turn_id") != turn_id
            or payload.get("fingerprint") != fingerprint
            or payload.get("commit_digest") != expected_digest
        ):
            raise CompletedTurnIntegrityError(
                "completed-turn locator identity does not match its ledger entry"
            )
        response = payload.get("response")
        if not isinstance(response, dict):
            raise CompletedTurnIntegrityError("completed-turn locator has no response object")
        actual_digest = self._commit_digest(turn_id, fingerprint, response)
        if actual_digest != expected_digest:
            raise CompletedTurnIntegrityError("completed-turn commit digest mismatch")
        if str(response.get("type", "error")) != outcome_kind:
            raise CompletedTurnIntegrityError("completed-turn outcome kind mismatch")
        self._cache(session, turn_id, response)
        return CompletedTurnResolution(_canonical_clone(response), "durable_locator")

    def record(
        self,
        session: Session,
        *,
        turn_id: str,
        fingerprint: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        """先持久化响应定位文件，再把紧凑身份写入 Session 工作副本。"""
        existing = self.resolve(session, turn_id=turn_id, fingerprint=fingerprint)
        if existing is not None:
            return dict(session.completed_turn_ledger[turn_id])
        response_copy = _canonical_clone(response)
        commit_digest = self._commit_digest(turn_id, fingerprint, response_copy)
        locator = self._locator(session.session_id, session.session_epoch, turn_id, commit_digest)
        atomic_write_json(
            self._root / locator,
            {
                "schema_version": 1,
                "session_epoch": session.session_epoch,
                "turn_id": turn_id,
                "fingerprint": fingerprint,
                "commit_digest": commit_digest,
                "response": response_copy,
            },
        )
        entry = {
            "fingerprint": fingerprint,
            "outcome_kind": str(response_copy.get("type", "error")),
            "commit_digest": commit_digest,
            "response_locator": locator.as_posix(),
        }
        session.completed_turn_ledger[turn_id] = entry
        self._cache(session, turn_id, response_copy)
        return dict(entry)

    def _cache(self, session: Session, turn_id: str, response: Mapping[str, Any]) -> None:
        """更新有界热缓存，而不删除权威账本身份。"""
        session.completed_response_hot_cache.pop(turn_id, None)
        session.completed_response_hot_cache[turn_id] = _canonical_clone(response)
        while len(session.completed_response_hot_cache) > self._hot_cache_size:
            oldest = next(iter(session.completed_response_hot_cache))
            del session.completed_response_hot_cache[oldest]

    def _load_locator(self, locator: str) -> dict[str, Any]:
        """读取并验证项目内的当前版响应定位文件。"""
        relative = Path(locator)
        if not locator or relative.is_absolute() or ".." in relative.parts:
            raise CompletedTurnIntegrityError("completed-turn locator is invalid")
        path = (self._root / relative).resolve()
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise CompletedTurnIntegrityError("completed-turn locator escaped its root") from exc
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CompletedTurnIntegrityError("completed-turn locator is unreadable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise CompletedTurnIntegrityError("unsupported completed-turn locator schema")
        return value

    @staticmethod
    def _locator(
        session_id: str,
        session_epoch: str,
        turn_id: str,
        commit_digest: str,
    ) -> Path:
        """为 Session epoch 与 turn 构造不暴露原始标识的相对定位路径。"""
        # 每一层保留 80 bit 摘要，避免 Windows 传统 MAX_PATH，同时不暴露原始 id。
        session_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
        epoch_key = hashlib.sha256(session_epoch.encode("utf-8")).hexdigest()[:20]
        turn_key = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:20]
        return Path(session_key, epoch_key, f"{turn_key}-{commit_digest[:16]}.json")

    @staticmethod
    def _commit_digest(
        turn_id: str,
        fingerprint: str,
        response: Mapping[str, Any],
    ) -> str:
        """计算绑定 turn、指纹与完整响应的提交摘要。"""
        return hashlib.sha256(
            _canonical_bytes(
                {
                    "turn_id": turn_id,
                    "fingerprint": fingerprint,
                    "response": dict(response),
                }
            )
        ).hexdigest()
