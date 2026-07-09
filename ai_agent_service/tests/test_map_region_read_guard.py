from __future__ import annotations

from app.api.schemas import FrontToolCallDTO
from app.query.helpers import (
    _blocks_platform_plan_after_empty_region_read,
    _remember_latest_map_region_read,
)
from app.sessions.store import Session


def test_platform_plan_is_blocked_after_empty_entry_region_read() -> None:
    session = Session(session_id="test")
    args = {
        "target_path": "TileMap",
        "map_layer": 0,
        "x": 65,
        "y": -25,
        "width": 15,
        "height": 39,
    }
    _remember_latest_map_region_read(
        session,
        args,
        {
            "ok": True,
            "target": "TileMap",
            "map_layer": 0,
            "map_revision": 7,
            "x": 65,
            "y": -25,
            "width": 15,
            "height": 39,
            "non_empty_count": 0,
        },
    )
    call = FrontToolCallDTO(
        id="call_plan",
        name="plan_reachable_map_growth",
        input={"profile": "platformer", **args},
        needs_confirm=False,
        frame_id="f1",
        agent="map-agent",
    )

    assert _blocks_platform_plan_after_empty_region_read(session, call) is True
