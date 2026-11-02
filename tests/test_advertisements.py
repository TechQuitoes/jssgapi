"""Tests for the Advertisement API."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.main import app
from app.models.group import SocialGroup
from app.models.user import Role, User

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
async def seed_data():
    from app.database import async_session_factory

    async with async_session_factory() as session:
        # Get or create super admin
        super_role = (
            await session.execute(select(Role).where(Role.name == "SUPER_ADMIN"))
        ).scalar_one()
        group_role = (
            await session.execute(select(Role).where(Role.name == "GROUP_ADMIN"))
        ).scalar_one()

        group_a = SocialGroup(name=f"Ad Group A {uuid.uuid4().hex[:6]}")
        group_b = SocialGroup(name=f"Ad Group B {uuid.uuid4().hex[:6]}")
        session.add_all([group_a, group_b])
        await session.flush()

        root = User(
            phone_number=f"+919{uuid.uuid4().int % 1000000000:09d}",
            full_name="Super Admin User",
            is_active=True,
            is_phone_verified=True,
            role_id=super_role.id,
        )
        admin_a = User(
            phone_number=f"+919{uuid.uuid4().int % 1000000000:09d}",
            full_name="Group Admin A",
            is_active=True,
            is_phone_verified=True,
            role_id=group_role.id,
            group_id=group_a.id,
        )
        session.add_all([root, admin_a])
        await session.flush()
        await session.commit()

        return {
            "group_a_id": group_a.id,
            "group_a_name": group_a.name,
            "group_b_id": group_b.id,
            "root_token": create_access_token(subject=str(root.id)),
            "admin_a_token": create_access_token(subject=str(admin_a.id)),
        }


def test_subscription_durations(client):
    """Verify the database-backed subscription plans and their prices."""
    response = client.get("/api/v1/advertisements/subscription-durations")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["months"] for item in data] == [1, 2, 3]
    assert [float(item["price"]) for item in data] == [2000.0, 4000.0, 6000.0]
    assert all(item["currency"] == "INR" for item in data)
    default_opt = next(item for item in data if item["months"] == 1)
    assert default_opt["is_default"] is True
    assert "1 Month" in default_opt["label"]


@pytest.mark.asyncio
async def test_super_admin_can_update_pricing(client, seed_data):
    """Only a Super Admin may change plan prices; existing snapshots stay intact."""
    seed = await seed_data
    root_headers = _auth(seed["root_token"])
    group_headers = _auth(seed["admin_a_token"])

    created = client.post(
        "/api/v1/advertisements",
        headers=root_headers,
        json={"title": "Snapshot Before Price Change"},
    )
    assert created.status_code == 201, created.text
    before = created.json()["data"]
    assert float(before["amount"]) == 2000.0

    forbidden = client.put(
        "/api/v1/advertisements/subscription-durations",
        headers=group_headers,
        json={"plans": [{"months": 1, "price": 2500}]},
    )
    assert forbidden.status_code == 403

    try:
        updated = client.put(
            "/api/v1/advertisements/subscription-durations",
            headers=root_headers,
            json={"plans": [{"months": 1, "price": 2500}]},
        )
        assert updated.status_code == 200, updated.text
        data = updated.json()["data"]
        first_plan = next(item for item in data if item["months"] == 1)
        assert float(first_plan["price"]) == 2500.0

        # The already purchased advertisement keeps its old snapshot.
        unchanged = client.get(
            f"/api/v1/advertisements/{before['id']}", headers=root_headers
        )
        assert unchanged.status_code == 200
        assert float(unchanged.json()["data"]["amount"]) == 2000.0

        new_ad = client.post(
            "/api/v1/advertisements",
            headers=root_headers,
            json={"title": "New Price Snapshot"},
        )
        assert new_ad.status_code == 201, new_ad.text
        assert float(new_ad.json()["data"]["amount"]) == 2500.0
    finally:
        client.put(
            "/api/v1/advertisements/subscription-durations",
            headers=root_headers,
            json={
                "plans": [
                    {"months": 1, "price": 2000},
                    {"months": 2, "price": 4000},
                    {"months": 3, "price": 6000},
                ]
            },
        )


@pytest.mark.asyncio
async def test_create_advertisement_json_super_admin(client, seed_data):
    """Super admin creates an advertisement with all screenshot fields."""
    seed = await seed_data
    token = seed["root_token"]
    payload = {
        "title": "Grand Jewellery Diwali Offer",
        "group_id": str(seed["group_a_id"]),
        "start_date": "2026-11-01T00:00:00Z",
        "end_date": "2026-12-15T00:00:00Z",
        "person_name": "Rajesh Shah",
        "mobile_number": "9876543210",
        "cover_image_url": "https://example.com/cover.jpg",
        "large_image_url": "https://example.com/large.jpg",
        "amount": 2000.00,
        "subscription_months": 1,
    }
    response = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json=payload,
    )
    assert response.status_code == 201, response.text
    ad = response.json()["data"]
    assert ad["title"] == payload["title"]
    assert ad["person_name"] == payload["person_name"]
    assert ad["mobile_number"] == payload["mobile_number"]
    assert ad["cover_image_url"] == payload["cover_image_url"]
    assert ad["large_image_url"] == payload["large_image_url"]
    assert float(ad["amount"]) == 2000.00
    assert ad["subscription_months"] == 1
    assert ad["currency"] == "INR"
    assert ad["subscription_plan_id"] is not None
    assert ad["purchased_at"] is not None
    assert ad["subscription_start_date"].startswith("2026-11-01")
    assert ad["subscription_expiry_date"].startswith("2026-12-01")
    assert ad["end_date"].startswith("2026-12-15")
    assert ad["is_active"] is True
    assert ad["status"] == "APPROVED"
    assert ad["group_name"] == seed["group_a_name"]


@pytest.mark.asyncio
async def test_create_advertisement_group_admin_auto_group(client, seed_data):
    """Group admin creates an advertisement; group is auto-scoped to their group."""
    seed = await seed_data
    token = seed["admin_a_token"]
    payload = {
        "title": "Community Business Promotion",
        "person_name": "Suresh Jain",
        "mobile_number": "9822000000",
        "subscription_months": 3,
    }
    response = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json=payload,
    )
    assert response.status_code == 201, response.text
    ad = response.json()["data"]
    assert ad["group_id"] == str(seed["group_a_id"])
    assert ad["group_name"] == seed["group_a_name"]
    assert ad["is_active"] is True
    assert ad["subscription_months"] == 3
    assert float(ad["amount"]) == 6000.0
    assert ad["subscription_expiry_date"] is not None
    assert ad["end_date"] is None


@pytest.mark.asyncio
async def test_group_admin_cannot_post_for_other_group(client, seed_data):
    """Group admin attempting to create ad for a foreign group is forbidden."""
    seed = await seed_data
    token = seed["admin_a_token"]
    payload = {
        "title": "Unauthorized Ad",
        "group_id": str(seed["group_b_id"]),
    }
    response = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json=payload,
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_create_rejects_tampered_amount(client, seed_data):
    """The client cannot choose a price; it must match the active plan."""
    seed = await seed_data
    response = client.post(
        "/api/v1/advertisements",
        headers=_auth(seed["root_token"]),
        json={"title": "Cheap Ad", "subscription_months": 1, "amount": 1},
    )
    assert response.status_code == 400
    assert "price has changed" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_cover_and_large_images(client, seed_data):
    """Test image upload endpoints for cover image and large image."""
    seed = await seed_data
    token = seed["root_token"]
    create_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json={"title": "Ad With Uploaded Images"},
    )
    assert create_res.status_code == 201
    ad_id = create_res.json()["data"]["id"]

    # Upload cover image
    cover_res = client.post(
        f"/api/v1/advertisements/{ad_id}/cover-image",
        headers=_auth(token),
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
    )
    assert cover_res.status_code == 201, cover_res.text
    assert cover_res.json()["data"]["cover_image_url"] is not None

    # Upload large image
    large_res = client.post(
        f"/api/v1/advertisements/{ad_id}/large-image",
        headers=_auth(token),
        files={"file": ("large.png", PNG_BYTES, "image/png")},
    )
    assert large_res.status_code == 201, large_res.text
    assert large_res.json()["data"]["large_image_url"] is not None

    # Delete cover image
    del_cover = client.delete(
        f"/api/v1/advertisements/{ad_id}/cover-image",
        headers=_auth(token),
    )
    assert del_cover.status_code == 200
    assert del_cover.json()["data"]["cover_image_url"] is None


@pytest.mark.asyncio
async def test_cover_image_max_size_enforcement(client, seed_data):
    """Cover image larger than 2MB is rejected."""
    seed = await seed_data
    token = seed["root_token"]
    create_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json={"title": "Size Test Ad"},
    )
    ad_id = create_res.json()["data"]["id"]

    # 2.5 MB content
    big_bytes = b"0" * (2500 * 1024)
    res = client.post(
        f"/api/v1/advertisements/{ad_id}/cover-image",
        headers=_auth(token),
        files={"file": ("big.png", big_bytes, "image/png")},
    )
    assert res.status_code == 400
    assert "2MB" in res.json().get("detail", {}).get("message", "")


@pytest.mark.asyncio
async def test_list_and_update_and_delete_advertisement(client, seed_data):
    """Test listing, updating, and deleting an advertisement."""
    seed = await seed_data
    token = seed["root_token"]
    create_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(token),
        json={"title": "To Be Updated"},
    )
    ad_id = create_res.json()["data"]["id"]

    # Update campaign fields only; the purchased snapshot must stay untouched.
    patch_res = client.patch(
        f"/api/v1/advertisements/{ad_id}",
        headers=_auth(token),
        json={"title": "Updated Title"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["data"]["title"] == "Updated Title"
    assert float(patch_res.json()["data"]["amount"]) == 2000.00
    assert patch_res.json()["data"]["subscription_months"] == 1

    # Tampering with the paid amount or duration is rejected.
    tampered = client.patch(
        f"/api/v1/advertisements/{ad_id}",
        headers=_auth(token),
        json={"amount": 500.00, "subscription_months": 6},
    )
    assert tampered.status_code == 400
    assert "locked" in tampered.json()["detail"].lower()

    # List active
    list_res = client.get(
        "/api/v1/advertisements",
        headers=_auth(token),
        params={"active_only": True},
    )
    assert list_res.status_code == 200
    items = list_res.json()["data"]
    assert any(i["id"] == ad_id for i in items)

    # Delete
    del_res = client.delete(
        f"/api/v1/advertisements/{ad_id}",
        headers=_auth(token),
    )
    assert del_res.status_code == 200

    # Get should now return 404
    get_res = client.get(f"/api/v1/advertisements/{ad_id}")
    assert get_res.status_code == 404


@pytest.mark.asyncio
async def test_list_advertisements_visible_to_group_admin_and_member(
    client, seed_data
):
    """Group Admin and Members see promotional banners just like Super Admin."""
    seed = await seed_data
    root_token = seed["root_token"]
    admin_a_token = seed["admin_a_token"]

    # 1. Create a global ad (group_id = None)
    global_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(root_token),
        json={"title": "Global Promo Banner"},
    )
    assert global_res.status_code == 201
    global_ad_id = global_res.json()["data"]["id"]

    # 2. Create an ad in Group A
    group_a_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(admin_a_token),
        json={"title": "Group A Banner"},
    )
    assert group_a_res.status_code == 201
    group_a_ad_id = group_a_res.json()["data"]["id"]

    # 3. Create an ad in Group B (different group)
    group_b_res = client.post(
        "/api/v1/advertisements",
        headers=_auth(root_token),
        json={"title": "Group B Banner", "group_id": str(seed["group_b_id"])},
    )
    assert group_b_res.status_code == 201
    group_b_ad_id = group_b_res.json()["data"]["id"]

    # 4. Group Admin A lists advertisements without group_id filter
    list_res = client.get(
        "/api/v1/advertisements",
        headers=_auth(admin_a_token),
    )
    assert list_res.status_code == 200
    item_ids = [item["id"] for item in list_res.json()["data"]]

    # Sees promotional banners on home screen just like Super Admin
    assert group_a_ad_id in item_ids
    assert global_ad_id in item_ids
    assert group_b_ad_id in item_ids

    # 5. When group_id is explicitly passed, it filters to that group (+ global)
    filtered_res = client.get(
        "/api/v1/advertisements",
        headers=_auth(admin_a_token),
        params={"group_id": str(seed["group_a_id"])},
    )
    assert filtered_res.status_code == 200
    filtered_ids = [item["id"] for item in filtered_res.json()["data"]]
    assert group_a_ad_id in filtered_ids
    assert global_ad_id in filtered_ids
    assert group_b_ad_id not in filtered_ids
