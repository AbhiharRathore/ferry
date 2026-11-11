import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import DeliveryStatus

EVENT_TYPE_PATTERN = r"^[a-zA-Z0-9_.\-]{1,100}$"


class AppCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class AppCreated(BaseModel):
    id: uuid.UUID
    name: str
    api_key: str = Field(description="Shown once. Store it; Ferry keeps only a hash.")


def _clean_event_types(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    if not v:
        raise ValueError("use null for all event types, not an empty list")
    for item in v:
        if not re.match(EVENT_TYPE_PATTERN, item):
            raise ValueError(f"invalid event type: {item!r}")
    return sorted(set(v))


class EndpointCreate(BaseModel):
    url: str = Field(max_length=2000)
    description: str | None = Field(default=None, max_length=200)
    event_types: list[str] | None = Field(default=None, description="Omit to receive every event type")

    _event_types = field_validator("event_types")(_clean_event_types)


class EndpointUpdate(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=200)
    event_types: list[str] | None = None
    enabled: bool | None = None

    _event_types = field_validator("event_types")(_clean_event_types)


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    description: str | None
    event_types: list[str] | None
    enabled: bool
    disabled_reason: str | None
    failure_streak: int
    created_at: datetime


class EndpointCreated(EndpointOut):
    secret: str = Field(description="Signing secret. Shown on creation only.")


class MessageCreate(BaseModel):
    event_type: str = Field(pattern=EVENT_TYPE_PATTERN)
    payload: dict[str, Any]


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_id: uuid.UUID
    endpoint_id: uuid.UUID
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    succeeded_at: datetime | None


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_no: int
    started_at: datetime
    duration_ms: int
    status_code: int | None
    error: str | None
    response_snippet: str | None


class DeliveryDetail(DeliveryOut):
    attempts: list[AttemptOut]


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    idempotency_key: str | None
    created_at: datetime
    deliveries: list[DeliveryOut]


class EndpointStats(BaseModel):
    endpoint_id: uuid.UUID
    deliveries_by_status: dict[str, int]
    attempts_last_24h: int
    success_rate_last_24h: float | None
    p50_latency_ms_last_24h: int | None
