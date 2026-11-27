from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MediaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_type: str
    owner_id: UUID
    # NULL for a personal file (a member profile photo) that is reached through
    # its owner rather than the group gallery.
    group_id: UUID | None = None
    file_name: str
    file_type: str
    mime_type: str
    file_size: int
    url: str
    thumb_url: str | None = None
    width: int | None = None
    height: int | None = None
    is_public: bool
    uploaded_by: UUID | None = None
    created_at: datetime
    updated_at: datetime
