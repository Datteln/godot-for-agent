"""Versioned, resumable WebSocket event protocol messages."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, TypeAlias

from pydantic import BaseModel, Field, TypeAdapter, model_validator

WEBSOCKET_PROTOCOL_VERSION: Final = 1


class ResumeMessage(BaseModel):
    type: Literal["resume"] = "resume"
    protocol_version: Literal[1] = 1
    session_id: str = Field(min_length=1, max_length=128)
    session_epoch: str = Field(min_length=1, max_length=128)
    after_seq: int = Field(ge=0)


class AckMessage(BaseModel):
    type: Literal["ack"] = "ack"
    protocol_version: Literal[1] = 1
    session_epoch: str = Field(min_length=1, max_length=128)
    accepted_seq: int = Field(ge=0)


class PongMessage(BaseModel):
    type: Literal["pong"] = "pong"
    protocol_version: Literal[1] = 1
    nonce: str = Field(min_length=1, max_length=128)


ClientSocketMessage: TypeAlias = Annotated[
    ResumeMessage | AckMessage | PongMessage,
    Field(discriminator="type"),
]


class HelloMessage(BaseModel):
    type: Literal["hello"] = "hello"
    protocol_version: Literal[1] = 1
    session_epoch: str
    high_water_seq: int = Field(ge=0)
    accepted_seq: int = Field(ge=0)
    resume_disposition: Literal["resumed", "current", "snapshot_required"]
    batch_event_limit: int = Field(gt=0)
    batch_byte_limit: int = Field(gt=0)
    unacked_event_limit: int = Field(gt=0)
    unacked_byte_limit: int = Field(gt=0)
    heartbeat_interval_s: float = Field(gt=0)


class SocketEvent(BaseModel):
    seq: int = Field(gt=0)
    session_id: str
    session_epoch: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    delivery: str | None = None
    provisional: bool = False
    preview_id: str | None = None
    request_id: str | None = None
    turn_id: str | None = None
    frame_id: str | None = None
    message_id: str | None = None


class EventBatchMessage(BaseModel):
    type: Literal["event_batch"] = "event_batch"
    protocol_version: Literal[1] = 1
    session_epoch: str
    first_seq: int = Field(gt=0)
    last_seq: int = Field(gt=0)
    events: list[SocketEvent] = Field(min_length=1)
    encoded_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_sequence(self) -> "EventBatchMessage":
        sequence = [item.seq for item in self.events]
        if sequence != list(range(self.first_seq, self.last_seq + 1)):
            raise ValueError("event_batch sequence must be contiguous")
        if any(item.session_epoch != self.session_epoch for item in self.events):
            raise ValueError("event_batch contains a foreign session epoch")
        return self


class EpochChangedMessage(BaseModel):
    type: Literal["epoch_changed"] = "epoch_changed"
    protocol_version: Literal[1] = 1
    previous_epoch: str
    new_epoch: str
    last_event_seq: int = Field(ge=0)


class SnapshotRequiredMessage(BaseModel):
    type: Literal["snapshot_required"] = "snapshot_required"
    protocol_version: Literal[1] = 1
    reason: Literal["cursor_expired", "sequence_gap", "stale_epoch", "invalid_epoch"]
    session_epoch: str
    high_water_seq: int = Field(ge=0)
    snapshot_path: Literal["/chat/snapshot"] = "/chat/snapshot"


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"
    protocol_version: Literal[1] = 1
    nonce: str


class CloseMessage(BaseModel):
    type: Literal["close"] = "close"
    protocol_version: Literal[1] = 1
    code: Literal[
        "invalid_message",
        "authentication_failed",
        "unsupported_protocol",
        "stale_epoch",
        "ack_out_of_range",
        "client_stalled",
        "event_too_large",
        "server_shutdown",
    ]
    retryable: bool
    resume_after_seq: int = Field(ge=0)


ServerSocketMessage: TypeAlias = Annotated[
    HelloMessage
    | EventBatchMessage
    | EpochChangedMessage
    | SnapshotRequiredMessage
    | PingMessage
    | CloseMessage,
    Field(discriminator="type"),
]

_CLIENT_ADAPTER = TypeAdapter(ClientSocketMessage)
_SERVER_ADAPTER = TypeAdapter(ServerSocketMessage)


def parse_client_message(payload: Any) -> ClientSocketMessage:
    return _CLIENT_ADAPTER.validate_python(payload)


def parse_server_message(payload: Any) -> ServerSocketMessage:
    return _SERVER_ADAPTER.validate_python(payload)
