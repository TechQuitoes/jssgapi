from __future__ import annotations

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict


class GuestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str
    phone_number: str
    email: str | None = None
    is_active: bool
    status: str
    role: str = "GUEST"
    tag: str = "GUEST"
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None


class GuestApproveRequest(BaseModel):
    status: str = "APPROVED"  # APPROVED or REJECTED
    rejection_reason: str | None = None
