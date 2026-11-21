from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


class DeviceTokenRegister(BaseModel):
    token: str
    platform: str = "android"
    device_name: str | None = None


class DeviceTokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    token: str
    platform: str
    device_name: str | None = None
    is_active: bool
    created_at: datetime


class NotificationCreate(BaseModel):
    recipient_id: UUID
    title: str
    body: str | None = None
    notification_type: str = "GENERAL"
    entity_type: str | None = None
    entity_id: UUID | None = None
    data: str | None = None


class NotificationSendRequest(BaseModel):
    recipient_id: UUID | None = None
    title: str
    body: str | None = None
    notification_type: str = "GENERAL"
    entity_type: str | None = None
    entity_id: UUID | None = None
    data: str | None = None


class NotificationBroadcastRequest(BaseModel):
    title: str
    body: str | None = None
    notification_type: str = "GENERAL"


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    recipient_id: UUID | None = None
    title: str
    body: str | None = None
    data: str | None = None
    notification_type: str
    entity_type: str | None = None
    entity_id: UUID | None = None
    is_read: bool
    read_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    category: str | None = None
    amount: float | None = None
    donor_name: str | None = None
    group_name: str | None = None
    receipt_no: str | None = None
    transaction_id: str | None = None
    order_id: str | None = None
    link: str | None = None

    @model_validator(mode="before")
    @classmethod
    def extract_meta_fields(cls, data: Any) -> Any:
        raw_data = getattr(data, "data", None) if not isinstance(data, dict) else data.get("data")
        if raw_data and isinstance(raw_data, str):
            try:
                parsed = json.loads(raw_data)
                if isinstance(parsed, dict):
                    if isinstance(data, dict):
                        for k, v in parsed.items():
                            if k not in data or data[k] is None:
                                data[k] = v
                        return data
                    elif hasattr(data, "__table__"):
                        d = {c.name: getattr(data, c.name, None) for c in data.__table__.columns}
                        for k, v in parsed.items():
                            d[k] = v
                        return d
            except Exception:
                pass
        return data
