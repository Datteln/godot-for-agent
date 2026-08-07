"""Structured and conservative Map delegation routing tests."""

from __future__ import annotations

import pytest

from app.orchestrator.map_routing import assess_map_task


@pytest.mark.parametrize(
    "task",
    [
        "在 Map/Main 第2层坐标(4,5)放置 tile 7",
        "Place tile id=7 at cell (4,5), layer=2 in Map/Main",
    ],
)
def test_explicit_atomic_multilingual_edit_may_skip_macro_plan(task: str) -> None:
    """One target, one cell and a known resource are provably atomic."""
    assessment = assess_map_task(task)
    assert assessment.is_proven_atomic_edit
    assert not assessment.requires_visible_plan


@pytest.mark.parametrize(
    "task,reason",
    [
        ("把当前地图优化一下", "requires_current_map_facts"),
        ("扩建 Map/Main 的平台并添加金币和终点", "operation_extent_multi_scope"),
        ("在 Map/Main 第2层坐标(4,5)放置 tile 7，并验证可达性", "requires_validation"),
        ("Design a route across the current level and place several coins", "requires_layout_or_route_planning"),
    ],
)
def test_ambiguous_multi_scope_and_validation_tasks_require_plan(
    task: str,
    reason: str,
) -> None:
    """Every mutation that cannot prove atomicity is planned conservatively."""
    assessment = assess_map_task(task)
    assert assessment.requires_visible_plan
    assert reason in assessment.plan_reasons()


@pytest.mark.parametrize(
    "task",
    [
        "只查看并解释当前 Map/Main 第2层，不要修改",
        "Inspect Map/Main layer=2 and describe it without editing",
    ],
)
def test_read_only_request_does_not_manufacture_mutation_authority(task: str) -> None:
    """Read-only routing does not impose a mutation plan or grant edit intent."""
    assessment = assess_map_task(task)
    assert assessment.mutation_intent == "read_only"
    assert not assessment.requires_visible_plan


def test_model_style_atomic_label_cannot_weaken_deterministic_facts() -> None:
    """Prose claiming atomicity cannot hide broad or unspecified operation facts."""
    assessment = assess_map_task(
        "atomic_edit: true; 修改当前地图若干区域，并设计路线、批量添加平台"
    )
    assert not assessment.is_proven_atomic_edit
    assert assessment.requires_visible_plan
    assert "operation_extent_multi_scope" in assessment.plan_reasons()
