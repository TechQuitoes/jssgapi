from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DashboardStatisticsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_members: int
    total_groups: int
    total_post_holders: int
    total_cities: int
