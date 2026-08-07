"""Canonical Chat Timeline 历史协议回归测试。"""

from app.api.schemas import (
    LogTextHistoryBlock,
    SessionHistoryResponse,
    ThoughtHistoryBlock,
    UserHistoryBlock,
)
from app.query.history_to_events import blocks_to_timeline_events


def test_history_records_are_canonical_and_share_live_message_identity() -> None:
    """历史正文与 reasoning 使用实时 Projector 可复现的身份和顺序键。"""
    records = blocks_to_timeline_events(
        [
            UserHistoryBlock(text="question", frame_id="f1", message_index=1),
            ThoughtHistoryBlock(
                header="Thought",
                detail="reason",
                frame_id="f1",
                message_index=2,
            ),
            LogTextHistoryBlock(
                text="answer",
                indent=True,
                frame_id="f1",
                message_index=2,
            ),
        ],
        session_epoch="epoch-1",
        start_index=8,
    )

    items = [record["payload"]["item"] for record in records]
    assert [item["item_id"] for item in items] == [
        "user:f1:1",
        "reasoning:f1:2",
        "assistant:f1:2",
    ]
    assert items[1]["order_key"] < items[2]["order_key"]
    assert all(record["type"] == "timeline_item" for record in records)
    assert "_history_" not in repr(records)


def test_history_timeline_records_contain_only_serializable_values() -> None:
    """历史事件可以直接跨 HTTP 传输且不携带运行时对象。"""
    records = blocks_to_timeline_events(
        [LogTextHistoryBlock(text="visible", frame_id="f1", message_index=3)],
        session_epoch="epoch-1",
        start_index=0,
    )

    assert records[0]["schema_version"] == 1
    assert records[0]["session_epoch"] == "epoch-1"
    assert records[0]["payload"]["item"]["content_blocks"] == [
        {"type": "markdown", "text": "visible"}
    ]


def test_history_response_exposes_events_without_pseudo_event_alias() -> None:
    """历史 API 只暴露 canonical events 字段且没有兼容别名。"""
    payload = SessionHistoryResponse(
        session_id="s1",
        session_epoch="epoch-1",
        events=[{"type": "timeline_item", "payload": {"item": {}}}],
    ).model_dump(mode="json")

    assert "events" in payload
    assert "pseudo_events" not in payload
