from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.advertisements import router as advertisements_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.celebrations import router as celebrations_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.events import router as events_router
from app.api.v1.family import router as family_router
from app.api.v1.fees import router as fees_router
from app.api.v1.geo import router as geo_router
from app.api.v1.groups import router as groups_router
from app.api.v1.jain_tirths import router as jain_tirths_router
from app.api.v1.locations import router as locations_router
from app.api.v1.media import router as media_router
from app.api.v1.members import router as members_router
from app.api.v1.news import router as news_router
from app.api.v1.notifications import router as notifications_router
from app.api.v1.payments import router as payments_router
from app.api.v1.professional import router as professional_router
from app.api.v1.profile import router as profile_router
from app.api.v1.search import router as search_router
from app.api.v1.translate import router as translate_router
from app.api.v1.trustees import router as trustees_router
from app.api.v1.users import router as users_router

v1_router = APIRouter()

for resource_router in [
    auth_router,
    users_router,
    locations_router,
    geo_router,
    groups_router,
    members_router,
    search_router,
    celebrations_router,
    trustees_router,
    jain_tirths_router,
    events_router,
    news_router,
    notifications_router,
    media_router,
    profile_router,
    advertisements_router,
    fees_router,
    payments_router,
    audit_router,
    dashboard_router,
]:
    v1_router.include_router(resource_router)

v1_router.include_router(family_router)
v1_router.include_router(professional_router)
v1_router.include_router(translate_router)
