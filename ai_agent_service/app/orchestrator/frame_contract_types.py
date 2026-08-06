"""定义领域 owner 与地图 specialist worker 的版本化 Frame 合同。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Final, Literal, TypeAlias, cast

from app.orchestrator.map_contracts import (
    MAP_WORKER_NEXT_STAGES,
    MAP_WORKER_RESULT_SCHEMA,
    MAP_WORKER_STAGES,
)
from app.orchestrator.map_planning_contexts import (
    MapExecutionOperation,
    MapPlanningContextBundle,
    MapPlanningContextEntry,
    MapPlanningContextError,
)

DOMAIN_OWNER_CONTRACT_KIND: Final = "domain_owner_v1"
MAP_WORKER_STAGE_CONTRACT_KIND: Final = "map_worker_stage_v1"
FRAME_CONTRACT_VERSION: Final = 1

MapWorkerStage: TypeAlias = Literal[
    "reader",
    "planner",
    "writer",
    "validator",
    "repairer",
    "reviewer",
]

_WORKER_ONLY_FIELDS: Final = frozenset(
    {
        "stage",
        "worker_instance_id",
        "result_schema",
        "allowed_next_stages",
        "target_path",
        "map_revision",
        "region",
        "approved_batch_ref",
        "approved_batch_id",
        "authoritative_snapshot",
        "planning_context_bundle",
        "execution_operations",
    }
)
_OWNER_ONLY_FIELDS: Final = frozenset(
    {
        "domain",
        "owner_frame_id",
        "parent_frame_id",
        "macro_step_id",
        "domain_task_id",
        "durable_task_id",
        "request_lineage_id",
        "accepted_publication_statuses",
    }
)


class FrameContractTypeError(ValueError):
    """表示 Frame 合同类型、版本或字段组合不合法。"""


def _optional_text(value: Any) -> str | None:
    """把非空字符串规范化为可选文本。"""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_int(value: Any) -> int | None:
    """把非布尔整数规范化为可选整数。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class DomainOwnerContract:
    """描述一个领域 owner Frame 的稳定身份与宏观任务链接。"""

    domain: str
    owner_frame_id: str
    parent_frame_id: str | None
    macro_step_id: str | None = None
    domain_task_id: str | None = None
    durable_task_id: str | None = None
    request_lineage_id: str | None = None
    accepted_publication_statuses: tuple[str, ...] = (
        "preview_ready",
        "awaiting_confirmation",
        "completed",
        "blocked",
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DomainOwnerContract:
        """校验并恢复一个版本化领域 owner 合同。"""
        if value.get("contract_kind") != DOMAIN_OWNER_CONTRACT_KIND:
            raise FrameContractTypeError("domain owner contract kind is invalid")
        if value.get("contract_version") != FRAME_CONTRACT_VERSION:
            raise FrameContractTypeError("domain owner contract version is invalid")
        forbidden = _WORKER_ONLY_FIELDS & set(value)
        if forbidden:
            raise FrameContractTypeError(
                "domain owner contract contains worker-only fields: " + ",".join(sorted(forbidden))
            )
        domain = _optional_text(value.get("domain"))
        owner_frame_id = _optional_text(value.get("owner_frame_id"))
        if domain is None or owner_frame_id is None:
            raise FrameContractTypeError("domain owner contract requires domain and owner_frame_id")
        raw_statuses = value.get("accepted_publication_statuses", ())
        if not isinstance(raw_statuses, (list, tuple)) or not all(
            isinstance(item, str) and item for item in raw_statuses
        ):
            raise FrameContractTypeError(
                "domain owner accepted_publication_statuses must be strings"
            )
        return cls(
            domain=domain,
            owner_frame_id=owner_frame_id,
            parent_frame_id=_optional_text(value.get("parent_frame_id")),
            macro_step_id=_optional_text(value.get("macro_step_id")),
            domain_task_id=_optional_text(value.get("domain_task_id")),
            durable_task_id=_optional_text(value.get("durable_task_id")),
            request_lineage_id=_optional_text(value.get("request_lineage_id")),
            accepted_publication_statuses=tuple(raw_statuses),
        )

    def with_macro_link(
        self,
        *,
        macro_step_id: str,
        domain_task_id: str,
    ) -> DomainOwnerContract:
        """返回绑定宏观步骤与领域任务身份的新 owner 合同。"""
        return replace(
            self,
            macro_step_id=macro_step_id,
            domain_task_id=domain_task_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """把领域 owner 合同序列化为稳定 JSON 字典。"""
        return {
            "contract_kind": DOMAIN_OWNER_CONTRACT_KIND,
            "contract_version": FRAME_CONTRACT_VERSION,
            "domain": self.domain,
            "owner_frame_id": self.owner_frame_id,
            "parent_frame_id": self.parent_frame_id,
            "macro_step_id": self.macro_step_id,
            "domain_task_id": self.domain_task_id,
            "durable_task_id": self.durable_task_id,
            "request_lineage_id": self.request_lineage_id,
            "accepted_publication_statuses": list(self.accepted_publication_statuses),
        }


@dataclass(frozen=True)
class MapWorkerStageContract:
    """描述一个地图 specialist worker Frame 的冻结执行范围。"""

    stage: MapWorkerStage
    target_path: str | None = None
    map_revision: int | None = None
    region: dict[str, int] | None = None
    approved_batch_ref: str | None = None
    approved_batch_id: str | None = None
    authoritative_snapshot: dict[str, Any] | None = None
    planning_context_bundle: MapPlanningContextBundle | None = None
    execution_operations: tuple[MapExecutionOperation, ...] = ()
    contract_id: str | None = None
    worker_instance_id: str | None = None
    result_schema: str | None = None
    allowed_next_stages: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MapWorkerStageContract:
        """校验并恢复 worker-stage 合同，兼容未加 discriminator 的旧输入。"""
        kind = value.get("contract_kind", MAP_WORKER_STAGE_CONTRACT_KIND)
        version = value.get("contract_version", FRAME_CONTRACT_VERSION)
        if kind != MAP_WORKER_STAGE_CONTRACT_KIND:
            raise FrameContractTypeError("map worker contract kind is invalid")
        if version != FRAME_CONTRACT_VERSION:
            raise FrameContractTypeError("map worker contract version is invalid")
        forbidden = _OWNER_ONLY_FIELDS & set(value)
        if forbidden:
            raise FrameContractTypeError(
                "map worker contract contains owner-only fields: " + ",".join(sorted(forbidden))
            )
        stage_value = value.get("stage")
        if not isinstance(stage_value, str) or stage_value not in MAP_WORKER_STAGES:
            raise FrameContractTypeError("map worker contract stage is invalid")
        raw_region = value.get("region")
        region = (
            {
                str(key): item
                for key, item in raw_region.items()
                if isinstance(item, int) and not isinstance(item, bool)
            }
            if isinstance(raw_region, dict)
            else None
        )
        raw_snapshot = value.get("authoritative_snapshot")
        snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, dict) else None
        raw_bundle = value.get("planning_context_bundle")
        try:
            if isinstance(raw_bundle, dict):
                planning_context_bundle = MapPlanningContextBundle.from_dict(raw_bundle)
            elif snapshot is not None:
                try:
                    planning_context_bundle = MapPlanningContextBundle.from_entries(
                        [MapPlanningContextEntry.from_snapshot(snapshot)]
                    )
                except MapPlanningContextError:
                    # 历史合同可能只有 snapshot_id；保留原字段完成往返，等 reader
                    # 产生完整 locator 后再迁移，不能凭空补 artifact/digest。
                    planning_context_bundle = None
            else:
                planning_context_bundle = None
            raw_operations = value.get("execution_operations", ())
            if not isinstance(raw_operations, (list, tuple)):
                raise MapPlanningContextError("execution_operations must be an array")
            execution_operations = tuple(
                MapExecutionOperation.from_dict(item)
                for item in raw_operations
                if isinstance(item, dict)
            )
            if len(execution_operations) != len(raw_operations):
                raise MapPlanningContextError("execution operations must be objects")
        except MapPlanningContextError as exc:
            raise FrameContractTypeError(str(exc)) from exc
        raw_next = value.get("allowed_next_stages", ())
        if not isinstance(raw_next, (list, tuple)) or not all(
            isinstance(item, str) for item in raw_next
        ):
            raise FrameContractTypeError("map worker allowed_next_stages must be strings")
        expected_next = tuple(sorted(MAP_WORKER_NEXT_STAGES[cast(MapWorkerStage, stage_value)]))
        normalized_next = tuple(raw_next)
        if normalized_next and normalized_next != expected_next:
            raise FrameContractTypeError(
                "map worker allowed_next_stages does not match the closed stage graph"
            )
        result_schema = _optional_text(value.get("result_schema"))
        if result_schema not in {None, MAP_WORKER_RESULT_SCHEMA}:
            raise FrameContractTypeError("map worker result schema is invalid")
        return cls(
            stage=cast(MapWorkerStage, stage_value),
            target_path=_optional_text(value.get("target_path")),
            map_revision=_optional_int(value.get("map_revision")),
            region=region,
            approved_batch_ref=_optional_text(value.get("approved_batch_ref")),
            approved_batch_id=_optional_text(value.get("approved_batch_id")),
            authoritative_snapshot=snapshot,
            planning_context_bundle=planning_context_bundle,
            execution_operations=execution_operations,
            contract_id=_optional_text(value.get("contract_id")),
            worker_instance_id=_optional_text(value.get("worker_instance_id")),
            result_schema=result_schema,
            allowed_next_stages=normalized_next,
        )

    def bind_runtime_identity(
        self,
        *,
        contract_id: str,
        worker_instance_id: str,
    ) -> MapWorkerStageContract:
        """返回绑定不可变运行时身份、schema 与下一阶段的 worker 合同。"""
        return replace(
            self,
            contract_id=contract_id,
            worker_instance_id=worker_instance_id,
            result_schema=MAP_WORKER_RESULT_SCHEMA,
            allowed_next_stages=tuple(sorted(MAP_WORKER_NEXT_STAGES[self.stage])),
        )

    def to_dict(self) -> dict[str, Any]:
        """把 worker-stage 合同序列化为稳定 JSON 字典。"""
        return {
            "contract_kind": MAP_WORKER_STAGE_CONTRACT_KIND,
            "contract_version": FRAME_CONTRACT_VERSION,
            "stage": self.stage,
            "target_path": self.target_path,
            "map_revision": self.map_revision,
            "region": dict(self.region) if self.region is not None else None,
            "approved_batch_ref": self.approved_batch_ref,
            "approved_batch_id": self.approved_batch_id,
            "authoritative_snapshot": (
                dict(self.authoritative_snapshot)
                if self.authoritative_snapshot is not None
                else None
            ),
            "planning_context_bundle": (
                self.planning_context_bundle.to_dict()
                if self.planning_context_bundle is not None
                else None
            ),
            "execution_operations": [
                operation.to_dict() for operation in self.execution_operations
            ],
            "contract_id": self.contract_id,
            "worker_instance_id": self.worker_instance_id,
            "result_schema": self.result_schema,
            "allowed_next_stages": list(self.allowed_next_stages),
        }


def contract_kind(value: dict[str, Any]) -> str | None:
    """返回已知 Frame 合同 discriminator，未知或缺失时返回 None。"""
    kind = value.get("contract_kind")
    return kind if isinstance(kind, str) and kind else None
