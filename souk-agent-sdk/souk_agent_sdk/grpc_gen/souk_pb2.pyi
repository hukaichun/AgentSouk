from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PollRequest(_message.Message):
    __slots__ = ("agent_ids", "max_claim", "wait_seconds")
    AGENT_IDS_FIELD_NUMBER: _ClassVar[int]
    MAX_CLAIM_FIELD_NUMBER: _ClassVar[int]
    WAIT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    agent_ids: _containers.RepeatedScalarFieldContainer[str]
    max_claim: int
    wait_seconds: int
    def __init__(self, agent_ids: _Optional[_Iterable[str]] = ..., max_claim: _Optional[int] = ..., wait_seconds: _Optional[int] = ...) -> None: ...

class PollResponse(_message.Message):
    __slots__ = ("pending",)
    PENDING_FIELD_NUMBER: _ClassVar[int]
    pending: _containers.RepeatedCompositeFieldContainer[PendingRun]
    def __init__(self, pending: _Optional[_Iterable[_Union[PendingRun, _Mapping]]] = ...) -> None: ...

class PendingRun(_message.Message):
    __slots__ = ("run_id", "agent_id", "json_payload")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    JSON_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    agent_id: str
    json_payload: str
    def __init__(self, run_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., json_payload: _Optional[str] = ...) -> None: ...

class AgentEventEnvelope(_message.Message):
    __slots__ = ("run_id", "agent_id", "json_payload", "end_of_stream", "cancel")
    RUN_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    JSON_PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    END_OF_STREAM_FIELD_NUMBER: _ClassVar[int]
    CANCEL_FIELD_NUMBER: _ClassVar[int]
    run_id: str
    agent_id: str
    json_payload: str
    end_of_stream: bool
    cancel: bool
    def __init__(self, run_id: _Optional[str] = ..., agent_id: _Optional[str] = ..., json_payload: _Optional[str] = ..., end_of_stream: _Optional[bool] = ..., cancel: _Optional[bool] = ...) -> None: ...
