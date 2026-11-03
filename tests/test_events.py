"""Tests for the events API.

Covers the shape of an event (host, type, wall-clock date and time), the
past/upcoming/future buckets, the cover image and the gallery of at most ten
images.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.constants import EventTimeFrame
from app.core.security import hash_password
from app.main import app
from app.models.event import Event
from app.models.event_image import EventImage
from app.models.group import SocialGroup
from app.models.media import Media, MediaObject
from app.models.user import Role, User

PASSWORD = "StrongPass123!"

#: A one pixel PNG, small enough to upload eleven times.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n\x2d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _psycopg_url() -> str:
    return get_settings().database_url.replace(
        "postgresql+asyncpg://", "postgresql+psycopg2://"
    )


def _next_phone() -> str:
    return f"+916{str(time.time_ns())[-9:]}"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_user(engine, role_name: str, phone: str, group_id=None) -> User:
    with Session(engine) as session:
        role = session.execute(
            select(Role).where(Role.name == role_name)
        ).scalar_one()
        user = User(
            phone_number=phone,
            password_hash=hash_password(PASSWORD),
            full_name=f"Event {role_name}",
            is_email_verified=True,
            is_phone_verified=True,
            role_id=role.id,
            group_id=group_id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def _create_group(engine, name: str) -> SocialGroup:
    with Session(engine) as session:
        group = SocialGroup(name=name)
        session.add(group)
        session.commit()
        session.refresh(group)
        return group


def _login(client: TestClient, phone: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"phone_number": phone, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["tokens"]["access_token"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(_psycopg_url(), poolclass=NullPool)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def world(client, engine):
    """Three groups, each with an admin, plus a platform admin."""
    group_a = _create_group(engine, f"Event Group A {uuid.uuid4()}")
    group_b = _create_group(engine, f"Event Group B {uuid.uuid4()}")
    group_c = _create_group(engine, f"Event Group C {uuid.uuid4()}")

    admin_a_phone = _next_phone()
    admin_b_phone = _next_phone()
    admin_c_phone = _next_phone()
    root_phone = _next_phone()

    admin_a = _create_user(engine, "GROUP_ADMIN", admin_a_phone, group_a.id)
    admin_b = _create_user(engine, "GROUP_ADMIN", admin_b_phone, group_b.id)
    admin_c = _create_user(engine, "GROUP_ADMIN", admin_c_phone, group_c.id)
    root = _create_user(engine, "SUPER_ADMIN", root_phone)

    return {
        "group_a": group_a.id,
        "group_b": group_b.id,
        "group_c": group_c.id,
        "admin_a_id": admin_a.id,
        "admin_b_id": admin_b.id,
        "admin_c_id": admin_c.id,
        "root_id": root.id,
        "tokens": {
            "admin_a": _login(client, admin_a_phone),
            "admin_b": _login(client, admin_b_phone),
            "admin_c": _login(client, admin_c_phone),
            "root": _login(client, root_phone),
        },
    }


def _event_payload(**overrides) -> dict:
    payload = {
        "title": "Annual Day",
        "event_date": "25/12/2026",
        "event_time": "18:30",
        "event_type": "ANNUAL",
    }
    payload.update(overrides)
    return payload


def _create_event(client, world, token: str, **overrides) -> dict:
    response = client.post(
        "/api/v1/events",
        json=_event_payload(**overrides),
        headers=_auth(world["tokens"][token]),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _make_ungrouped_event(client, world, engine, title: str) -> dict:
    event = _create_event(
        client,
        world,
        "root",
        title=title,
        group_ids=[str(world["group_a"])],
    )
    with Session(engine) as session:
        stored = session.get(Event, uuid.UUID(event["id"]))
        assert stored is not None
        stored.group_id = None
        stored.event_groups.clear()
        session.commit()
    return event


# --- host defaults --------------------------------------------------------


def test_create_fills_the_host_from_the_uploader(client, world, engine):
    event = _create_event(client, world, "admin_a")

    assert event["host_id"] == str(world["admin_a_id"])
    assert event["host_name"] == "Event GROUP_ADMIN"
    assert event["host_contact_number"] is not None


def test_an_explicit_host_overrides_the_default(client, world):
    event = _create_event(
        client,
        world,
        "admin_a",
        host_name="Someone Else",
        host_contact_number="+919999999999",
    )

    assert event["host_name"] == "Someone Else"
    assert event["host_contact_number"] == "+919999999999"


# --- date and time --------------------------------------------------------


def test_the_date_keeps_the_dd_mm_yyyy_shape(client, world):
    event = _create_event(client, world, "admin_a")

    assert event["event_date"] == "25/12/2026"
    assert event["event_time"] == "18:30"


def test_an_iso_date_is_accepted_and_returned_as_dd_mm_yyyy(client, world):
    event = _create_event(client, world, "admin_a", event_date="2026-12-25")

    assert event["event_date"] == "25/12/2026"


def test_a_date_without_a_time_is_rejected(client, world):
    response = client.post(
        "/api/v1/events",
        json={"title": "No time", "event_date": "25/12/2026"},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 422


def test_a_nonsense_date_is_rejected(client, world):
    response = client.post(
        "/api/v1/events",
        json=_event_payload(event_date="not-a-date"),
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 422


def test_the_instant_follows_the_wall_clock_values(client, world):
    event = _create_event(client, world, "admin_a")

    # 18:30 in Asia/Kolkata is 13:00 UTC.
    assert event["starts_at"].startswith("2026-12-25T13:00")


# --- event type -----------------------------------------------------------


def test_a_named_type_needs_no_free_text_label(client, world):
    event = _create_event(client, world, "admin_a", event_type="CULTURAL_PROGRAM")

    assert event["event_type"] == "CULTURAL_PROGRAM"
    assert event["event_type_other"] is None


def test_the_other_type_needs_a_free_text_label(client, world):
    response = client.post(
        "/api/v1/events",
        json=_event_payload(event_type="OTHER"),
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 422


def test_the_other_type_keeps_its_label(client, world):
    event = _create_event(
        client,
        world,
        "admin_a",
        event_type="OTHER",
        event_type_other="Holi Special",
    )

    assert event["event_type"] == "OTHER"
    assert event["event_type_other"] == "Holi Special"


def test_switching_to_a_named_type_drops_the_label(client, world):
    event = _create_event(
        client,
        world,
        "admin_a",
        event_type="OTHER",
        event_type_other="Holi Special",
    )

    response = client.patch(
        f"/api/v1/events/{event['id']}",
        json={"event_type": "RELIGIOUS_PROGRAM"},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["event_type_other"] is None


def test_the_event_types_endpoint_lists_every_choice(client, world):
    response = client.get(
        "/api/v1/events/event-types", headers=_auth(world["tokens"]["admin_a"])
    )

    assert response.status_code == 200
    values = {item["value"] for item in response.json()["data"]}
    assert {"ANNUAL", "OTHER", "CULTURAL_PROGRAM"} <= values


# --- time frames ----------------------------------------------------------


def test_an_event_in_the_past_is_past(client, world):
    yesterday = (time.time() - 86400)
    payload_date = time.strftime("%d/%m/%Y", time.localtime(yesterday))
    event = _create_event(client, world, "admin_a", event_date=payload_date)

    assert event["time_frame"] == EventTimeFrame.PAST.value


def test_an_event_within_seven_days_is_upcoming(client, world):
    in_three_days = time.time() + 3 * 86400
    payload_date = time.strftime("%d/%m/%Y", time.localtime(in_three_days))
    event = _create_event(client, world, "admin_a", event_date=payload_date)

    assert event["time_frame"] == EventTimeFrame.UPCOMING.value


def test_an_event_beyond_seven_days_is_future(client, world):
    in_thirty_days = time.time() + 30 * 86400
    payload_date = time.strftime("%d/%m/%Y", time.localtime(in_thirty_days))
    event = _create_event(client, world, "admin_a", event_date=payload_date)

    assert event["time_frame"] == EventTimeFrame.FUTURE.value


def test_the_time_frame_filter_narrows_the_list(client, world):
    in_thirty_days = time.time() + 30 * 86400
    payload_date = time.strftime("%d/%m/%Y", time.localtime(in_thirty_days))
    future = _create_event(
        client, world, "admin_a", title="Far Future", event_date=payload_date
    )

    response = client.get(
        "/api/v1/events?time_frame=future",
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert future["id"] in ids
    assert all(
        item["time_frame"] == EventTimeFrame.FUTURE.value
        for item in response.json()["data"]
    )


def test_the_event_type_filter_narrows_the_list(client, world):
    created = _create_event(
        client, world, "admin_a", title="Fundraiser", event_type="FUNDRAISER"
    )

    response = client.get(
        "/api/v1/events?event_type=FUNDRAISER",
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert created["id"] in ids
    assert all(
        item["event_type"] == "FUNDRAISER" for item in response.json()["data"]
    )


def test_the_search_filter_narrows_the_list(client, world):
    created = _create_event(client, world, "admin_a", title="Unique Ceremony")

    response = client.get(
        "/api/v1/events?search=Unique", headers=_auth(world["tokens"]["admin_a"])
    )

    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["data"]}
    assert created["id"] in ids


# --- cover image ----------------------------------------------------------


def test_the_cover_image_is_stored_on_the_event(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.post(
        f"/api/v1/events/{event['id']}/cover",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["cover_image_url"]


def test_the_cover_image_can_be_replaced(client, world):
    event = _create_event(client, world, "admin_a")

    first = client.post(
        f"/api/v1/events/{event['id']}/cover",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
        headers=_auth(world["tokens"]["admin_a"]),
    )
    second = client.post(
        f"/api/v1/events/{event['id']}/cover",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"]["cover_image_url"] != first.json()["data"][
        "cover_image_url"
    ]


def test_the_cover_image_can_be_removed(client, world):
    event = _create_event(client, world, "admin_a")
    client.post(
        f"/api/v1/events/{event['id']}/cover",
        files={"file": ("cover.png", PNG_BYTES, "image/png")},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    response = client.delete(
        f"/api/v1/events/{event['id']}/cover",
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200
    detail = client.get(
        f"/api/v1/events/{event['id']}", headers=_auth(world["tokens"]["admin_a"])
    )
    assert detail.json()["data"]["cover_image_url"] is None


def test_a_cover_that_is_not_an_image_is_rejected(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.post(
        f"/api/v1/events/{event['id']}/cover",
        files={"file": ("cover.txt", b"not an image", "text/plain")},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 400


# --- gallery --------------------------------------------------------------


def test_images_can_be_uploaded_together(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[
            ("files", ("one.png", PNG_BYTES, "image/png")),
            ("files", ("two.png", PNG_BYTES, "image/png")),
        ],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200, response.text
    images = response.json()["data"]
    assert len(images) == 2
    assert [image["position"] for image in images] == [0, 1]


def test_the_gallery_is_returned_with_the_event(client, world):
    event = _create_event(client, world, "admin_a")
    client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[("files", ("one.png", PNG_BYTES, "image/png"))],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    response = client.get(
        f"/api/v1/events/{event['id']}", headers=_auth(world["tokens"]["admin_a"])
    )

    assert response.status_code == 200
    detail = response.json()["data"]
    assert detail["image_count"] == 1
    assert len(detail["images"]) == 1


def test_an_event_holds_at_most_ten_images(client, world):
    event = _create_event(client, world, "admin_a")

    for _ in range(5):
        response = client.post(
            f"/api/v1/events/{event['id']}/images",
            files=[
                ("files", ("one.png", PNG_BYTES, "image/png")),
                ("files", ("two.png", PNG_BYTES, "image/png")),
            ],
            headers=_auth(world["tokens"]["admin_a"]),
        )
        assert response.status_code == 200, response.text

    listing = client.get(
        f"/api/v1/events/{event['id']}/images",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert len(listing.json()["data"]) == 10


def test_the_eleventh_image_is_rejected(client, world):
    event = _create_event(client, world, "admin_a")
    for _ in range(5):
        client.post(
            f"/api/v1/events/{event['id']}/images",
            files=[
                ("files", ("one.png", PNG_BYTES, "image/png")),
                ("files", ("two.png", PNG_BYTES, "image/png")),
            ],
            headers=_auth(world["tokens"]["admin_a"]),
        )

    response = client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[("files", ("eleven.png", PNG_BYTES, "image/png"))],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "TOO_MANY_IMAGES"


def test_an_oversized_batch_is_rejected_before_anything_is_stored(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[
            ("files", (f"{index}.png", PNG_BYTES, "image/png"))
            for index in range(11)
        ],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 400
    listing = client.get(
        f"/api/v1/events/{event['id']}/images",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert listing.json()["data"] == []


def test_deleting_an_image_frees_a_slot(client, world):
    event = _create_event(client, world, "admin_a")
    for _ in range(5):
        client.post(
            f"/api/v1/events/{event['id']}/images",
            files=[
                ("files", ("one.png", PNG_BYTES, "image/png")),
                ("files", ("two.png", PNG_BYTES, "image/png")),
            ],
            headers=_auth(world["tokens"]["admin_a"]),
        )

    listing = client.get(
        f"/api/v1/events/{event['id']}/images",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    image_id = listing.json()["data"][0]["id"]

    deleted = client.delete(
        f"/api/v1/events/{event['id']}/images/{image_id}",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert deleted.status_code == 200

    added = client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[("files", ("extra.png", PNG_BYTES, "image/png"))],
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert added.status_code == 200


def test_a_file_that_is_not_an_image_is_rejected(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[("files", ("note.txt", b"hello", "text/plain"))],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 400


# --- access ---------------------------------------------------------------


def test_another_group_cannot_see_the_event(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.get(
        f"/api/v1/events/{event['id']}", headers=_auth(world["tokens"]["admin_b"])
    )

    assert response.status_code == 404


def test_another_group_cannot_upload_images(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[("files", ("one.png", PNG_BYTES, "image/png"))],
        headers=_auth(world["tokens"]["admin_b"]),
    )

    assert response.status_code == 403


def test_a_member_may_not_create_an_event(client, world, engine):
    phone = _next_phone()
    member = _create_user(engine, "MEMBER", phone)
    token = _login(client, phone)

    response = client.post(
        "/api/v1/events",
        json=_event_payload(),
        headers=_auth(token),
    )

    assert response.status_code == 403
    assert member.id is not None


# --- updates --------------------------------------------------------------


def test_clearing_a_required_field_is_rejected(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.patch(
        f"/api/v1/events/{event['id']}",
        json={"title": None},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 422


def test_the_date_can_be_moved(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.patch(
        f"/api/v1/events/{event['id']}",
        json={"event_date": "01/01/2027"},
        headers=_auth(world["tokens"]["admin_a"]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["event_date"] == "01/01/2027"


# --- deletion -------------------------------------------------------------


def test_a_deleted_event_disappears(client, world):
    event = _create_event(client, world, "admin_a")

    deleted = client.delete(
        f"/api/v1/events/{event['id']}", headers=_auth(world["tokens"]["root"])
    )
    assert deleted.status_code == 200

    fetched = client.get(
        f"/api/v1/events/{event['id']}", headers=_auth(world["tokens"]["admin_a"])
    )
    assert fetched.status_code == 404


def test_a_deleted_event_leaves_the_listing(client, world):
    event = _create_event(client, world, "admin_a")
    client.delete(
        f"/api/v1/events/{event['id']}", headers=_auth(world["tokens"]["root"])
    )

    response = client.get(
        "/api/v1/events?search=Annual Day", headers=_auth(world["tokens"]["admin_a"])
    )

    ids = {item["id"] for item in response.json()["data"]}
    assert event["id"] not in ids


def test_deleting_an_event_removes_its_gallery(client, world, engine):
    event = _create_event(client, world, "admin_a")
    client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[("files", ("one.png", PNG_BYTES, "image/png"))],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    client.delete(
        f"/api/v1/events/{event['id']}", headers=_auth(world["tokens"]["root"])
    )

    with engine.connect() as connection:
        remaining = connection.execute(
            select(EventImage).where(
                EventImage.event_id == uuid.UUID(event["id"]),
                EventImage.is_deleted.is_(False),
            )
        ).scalars().all()
        assert remaining == []


# --- housekeeping ---------------------------------------------------------


def test_the_events_table_has_the_new_columns(client, world, engine):
    with engine.connect() as connection:
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns("events")}
    assert {
        "event_date",
        "event_time",
        "event_type",
        "event_type_other",
        "host_id",
        "host_name",
        "host_contact_number",
        "cover_image_object_key",
    } <= columns


def test_the_media_rows_are_released_with_the_event(client, world, engine):
    event = _create_event(client, world, "admin_a")
    client.post(
        f"/api/v1/events/{event['id']}/images",
        files=[("files", ("one.png", PNG_BYTES, "image/png"))],
        headers=_auth(world["tokens"]["admin_a"]),
    )

    with engine.connect() as connection:
        object_keys = connection.execute(
            select(Media.r2_object_key).where(
                Media.owner_type == "event",
                Media.owner_id == uuid.UUID(event["id"]),
            )
        ).scalars().all()
    assert len(object_keys) == 1

    client.delete(
        f"/api/v1/events/{event['id']}", headers=_auth(world["tokens"]["root"])
    )

    with engine.connect() as connection:
        live = connection.execute(
            select(Media.id).where(
                Media.owner_type == "event",
                Media.owner_id == uuid.UUID(event["id"]),
                Media.is_deleted.is_(False),
            )
        ).scalars().all()
        assert live == []
        blobs = connection.execute(
            select(MediaObject.object_key).where(
                MediaObject.object_key.in_(object_keys)
            )
        ).scalars().all()
        assert blobs == []

# --- multiple groups ------------------------------------------------------


def test_an_event_can_be_shared_with_several_groups(client, world):
    title = f"Shared event {uuid.uuid4()}"
    event = _create_event(
        client,
        world,
        "root",
        title=title,
        group_ids=[str(world["group_a"]), str(world["group_b"])],
    )

    assert event["group_id"] == str(world["group_a"])
    assert set(event["group_ids"]) == {str(world["group_a"]), str(world["group_b"])}

    for token in ("admin_a", "admin_b"):
        response = client.get(
            f"/api/v1/events/{event['id']}",
            headers=_auth(world["tokens"][token]),
        )
        assert response.status_code == 200, response.text
        assert set(response.json()["data"]["group_ids"]) == {
            str(world["group_a"]),
            str(world["group_b"]),
        }

    response = client.get(
        f"/api/v1/events/{event['id']}",
        headers=_auth(world["tokens"]["admin_c"]),
    )
    assert response.status_code == 404, response.text

    response = client.get(
        f"/api/v1/events/{event['id']}/images",
        headers=_auth(world["tokens"]["admin_c"]),
    )
    assert response.status_code == 404, response.text


def test_a_list_only_contains_events_for_the_callers_groups(client, world):
    title = f"Scoped shared event {uuid.uuid4()}"
    event = _create_event(
        client,
        world,
        "root",
        title=title,
        group_ids=[str(world["group_a"]), str(world["group_b"])],
    )

    response = client.get(
        f"/api/v1/events?search={title}&page_size=100",
        headers=_auth(world["tokens"]["admin_c"]),
    )
    assert response.status_code == 200, response.text
    assert event["id"] not in {item["id"] for item in response.json()["data"]}

    response = client.get(
        f"/api/v1/events?search={title}&page_size=100",
        headers=_auth(world["tokens"]["admin_b"]),
    )
    assert response.status_code == 200, response.text
    assert event["id"] in {item["id"] for item in response.json()["data"]}


def test_updating_the_selection_changes_who_can_see_the_event(client, world):
    title = f"Reassigned event {uuid.uuid4()}"
    event = _create_event(
        client,
        world,
        "root",
        title=title,
        group_ids=[str(world["group_a"])],
    )

    response = client.patch(
        f"/api/v1/events/{event['id']}",
        json={"group_ids": [str(world["group_b"])]},
        headers=_auth(world["tokens"]["root"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["group_id"] == str(world["group_b"])
    assert response.json()["data"]["group_ids"] == [str(world["group_b"])]

    response = client.get(
        f"/api/v1/events/{event['id']}",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert response.status_code == 404, response.text

    response = client.get(
        f"/api/v1/events/{event['id']}",
        headers=_auth(world["tokens"]["admin_b"]),
    )
    assert response.status_code == 200, response.text


def test_group_admin_cannot_move_an_event_to_another_group(client, world):
    event = _create_event(client, world, "admin_a")

    response = client.patch(
        f"/api/v1/events/{event['id']}",
        json={"group_ids": [str(world["group_b"])]},
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert response.status_code == 403, response.text


def test_a_new_event_needs_at_least_one_group(client, world):
    response = client.post(
        "/api/v1/events",
        json=_event_payload(),
        headers=_auth(world["tokens"]["root"]),
    )
    assert response.status_code == 400, response.text


def test_super_admin_sees_events_from_every_group(client, world):
    marker = f"Super Admin View {uuid.uuid4()}"
    event_a = _create_event(client, world, "admin_a", title=f"{marker} A")
    event_b = _create_event(client, world, "admin_b", title=f"{marker} B")

    response = client.get(
        f"/api/v1/events?search={marker}&page_size=100",
        headers=_auth(world["tokens"]["root"]),
    )

    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["data"]}
    assert {event_a["id"], event_b["id"]} <= ids


def test_super_admin_group_query_does_not_narrow_the_list(client, world):
    title = f"Unfiltered Super Admin {uuid.uuid4()}"
    event = _create_event(
        client,
        world,
        "root",
        title=title,
        group_ids=[str(world["group_b"])],
    )

    response = client.get(
        f"/api/v1/events?group_id={world['group_a']}&search={title}",
        headers=_auth(world["tokens"]["root"]),
    )

    assert response.status_code == 200, response.text
    assert event["id"] in {item["id"] for item in response.json()["data"]}


def test_legacy_ungrouped_events_remain_visible_to_super_admin(client, world, engine):
    title = f"Legacy Ungrouped {uuid.uuid4()}"
    event = _make_ungrouped_event(client, world, engine, title)

    root_list = client.get(
        f"/api/v1/events?search={title}",
        headers=_auth(world["tokens"]["root"]),
    )
    assert root_list.status_code == 200, root_list.text
    assert event["id"] in {item["id"] for item in root_list.json()["data"]}

    admin_list = client.get(
        f"/api/v1/events?search={title}",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert admin_list.status_code == 200, admin_list.text
    assert event["id"] not in {item["id"] for item in admin_list.json()["data"]}

    admin_detail = client.get(
        f"/api/v1/events/{event['id']}",
        headers=_auth(world["tokens"]["admin_a"]),
    )
    assert admin_detail.status_code == 404, admin_detail.text


def test_members_only_see_events_for_their_groups(client, world, engine):
    phone = _next_phone()
    _create_user(engine, "MEMBER", phone, world["group_a"])
    token = _login(client, phone)

    marker = f"Member Visibility {uuid.uuid4()}"
    event_a = _create_event(client, world, "admin_a", title=f"{marker} A")
    event_b = _create_event(client, world, "admin_b", title=f"{marker} B")

    response = client.get(
        f"/api/v1/events?search={marker}&page_size=100",
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["data"]}
    assert event_a["id"] in ids
    assert event_b["id"] not in ids

    detail = client.get(
        f"/api/v1/events/{event_b['id']}",
        headers=_auth(token),
    )
    assert detail.status_code == 404, detail.text

