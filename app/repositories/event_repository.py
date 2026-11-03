from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select

from app.core.constants import EventTimeFrame, EventType
from app.models.event import Event
from app.models.event_group import EventGroup
from app.repositories.base_repository import BaseRepository


class EventRepository(BaseRepository[Event]):
    model = Event

    async def get_by_id(self, instance_id: UUID) -> Event | None:
        """Fetch a live event.

        A deleted event is gone as far as the API is concerned, so the soft
        delete flag is part of the lookup rather than something the caller has
        to remember to check.
        """
        result = await self.session.execute(
            select(Event).where(
                Event.id == instance_id,
                Event.is_deleted.is_(False),
            )
        )
        return result.scalar_one_or_none()

    def _clauses(
        self,
        *,
        group_ids: set[UUID] | None,
        include_ungrouped: bool,
        time_frame: EventTimeFrame | None,
        event_type: EventType | None,
        search: str | None,
        now: datetime,
        window_end: datetime,
        public_only: bool = False,
    ) -> list:
        clauses: list[Any] = [Event.is_deleted.is_(False)]
        if group_ids is not None:
            group_match = or_(
                Event.group_id.in_(group_ids),
                Event.event_groups.any(EventGroup.group_id.in_(group_ids)),
            )
            if include_ungrouped:
                # An all-groups event has group_id IS NULL (or no group selection).
                ungrouped = or_(Event.group_id.is_(None), ~Event.event_groups.any())
                clauses.append(or_(group_match, ungrouped))
            else:
                clauses.append(group_match)
        elif not include_ungrouped:
            clauses.append(
                or_(Event.group_id.is_not(None), Event.event_groups.any())
            )
        if time_frame is not None:
            if time_frame is EventTimeFrame.PAST:
                clauses.append(Event.starts_at < now)
            elif time_frame is EventTimeFrame.UPCOMING:
                clauses.append(Event.starts_at >= now)
                clauses.append(Event.starts_at <= window_end)
            else:
                clauses.append(Event.starts_at > window_end)
        if event_type is not None:
            clauses.append(Event.event_type == event_type.value)
        if search:
            term = f"%{search.strip()}%"
            clauses.append(
                or_(
                    Event.title.ilike(term),
                    Event.description.ilike(term),
                    Event.venue_name.ilike(term),
                )
            )
        if public_only:
            clauses.append(Event.is_public.is_(True))
        return clauses

    def _statement(
        self,
        *,
        group_ids: set[UUID] | None,
        include_ungrouped: bool = True,
        time_frame: EventTimeFrame | None,
        event_type: EventType | None,
        search: str | None,
        now: datetime,
        window_end: datetime,
        public_only: bool = False,
        order: str = "desc",
    ) -> Select:
        stmt = select(Event).where(
            *self._clauses(
                group_ids=group_ids,
                include_ungrouped=include_ungrouped,
                time_frame=time_frame,
                event_type=event_type,
                search=search,
                now=now,
                window_end=window_end,
                public_only=public_only,
            )
        )
        ordering = Event.starts_at.asc() if order == "asc" else Event.starts_at.desc()
        return stmt.order_by(ordering)

    async def list_filtered(
        self,
        *,
        group_ids: set[UUID] | None = None,
        include_ungrouped: bool = True,
        time_frame: EventTimeFrame | None = None,
        event_type: EventType | None = None,
        search: str | None = None,
        now: datetime,
        window_end: datetime,
        public_only: bool = False,
        order: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> list[Event]:
        stmt = self._statement(
            group_ids=group_ids,
            include_ungrouped=include_ungrouped,
            time_frame=time_frame,
            event_type=event_type,
            search=search,
            now=now,
            window_end=window_end,
            public_only=public_only,
            order=order,
        )
        result = await self.session.execute(stmt.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def count_filtered(
        self,
        *,
        group_ids: set[UUID] | None = None,
        include_ungrouped: bool = True,
        time_frame: EventTimeFrame | None = None,
        event_type: EventType | None = None,
        search: str | None = None,
        now: datetime,
        window_end: datetime,
        public_only: bool = False,
    ) -> int:
        stmt = select(func.count()).select_from(Event).where(
            *self._clauses(
                group_ids=group_ids,
                include_ungrouped=include_ungrouped,
                time_frame=time_frame,
                event_type=event_type,
                search=search,
                now=now,
                window_end=window_end,
                public_only=public_only,
            )
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())
