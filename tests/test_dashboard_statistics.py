from __future__ import annotations

import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models.user import Role, User

PASSWORD = "StrongPass123!"


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    return f"+916{str(time.time_ns())[-9:]}"


def _role_id(session: Session, role_name: str) -> UUID:
    return session.execute(
        select(Role.id).where(Role.name == role_name)
    ).scalar_one()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def super_admin(client: TestClient) -> str:
    engine = create_engine(_psycopg_url(), poolclass=NullPool)
    phone = _next_phone()
    try:
        with Session(engine) as session:
            user = User(
                phone_number=phone,
                password_hash=hash_password(PASSWORD),
                full_name="Dashboard Statistics Admin",
                is_email_verified=True,
                is_phone_verified=True,
                role_id=_role_id(session, "SUPER_ADMIN"),
            )
            session.add(user)
            session.commit()

        response = client.post(
            "/api/v1/auth/login",
            json={"phone_number": phone, "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        yield response.json()["tokens"]["access_token"]
    finally:
        with Session(engine) as session:
            session.execute(delete(User).where(User.phone_number == phone))
            session.commit()
        engine.dispose()


@pytest.fixture(scope="module")
def auth(super_admin: str) -> dict:
    return {"Authorization": f"Bearer {super_admin}"}


def test_dashboard_statistics_requires_auth(client: TestClient):
    response = client.get("/api/v1/dashboard/statistics")
    assert response.status_code == 401


def test_dashboard_statistics_returns_counts(client: TestClient, auth: dict):
    response = client.get("/api/v1/dashboard/statistics", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Dashboard statistics fetched successfully"
    data = body["data"]

    # Must contain only these 4 counts
    assert set(data.keys()) == {
        "total_members",
        "total_groups",
        "total_post_holders",
        "total_cities",
    }
    assert isinstance(data["total_members"], int)
    assert isinstance(data["total_groups"], int)
    assert isinstance(data["total_post_holders"], int)
    assert isinstance(data["total_cities"], int)
    assert data["total_members"] >= 0
    assert data["total_groups"] >= 0
    assert data["total_post_holders"] >= 0
    assert data["total_cities"] >= 0


def test_dashboard_statistics_alias(client: TestClient, auth: dict):
    response1 = client.get("/api/v1/dashboard/statistics", headers=auth)
    response2 = client.get("/api/v1/dashboard/stats", headers=auth)
    assert response1.status_code == 200
    assert response2.status_code == 200
    assert response1.json()["data"] == response2.json()["data"]
