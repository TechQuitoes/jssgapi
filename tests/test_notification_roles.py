from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.notification import Notification
from app.models.user import Role, User
from app.repositories.notification_repository import NotificationRepository
from app.services.notification_service import (
    create_payment_notification,
)


def _make_user(role_name: str, group_id: uuid.UUID | None = None) -> User:
    role = Role(name=role_name)
    user = User(
        id=uuid.uuid4(),
        full_name=f"User {role_name}",
        phone_number=f"+9198765{uuid.uuid4().hex[:5]}",
        password_hash="fakehash",
        role=role,
        role_id=role.id,
        group_id=group_id,
    )
    return user


def test_member_notification_access():
    """Member can only see their own notification (when they make a contribution or direct message),
    and can NEVER see admin notifications, group payments, other members' contributions, or ads.
    """
    session = MagicMock()
    repo = NotificationRepository(session)

    member_user = _make_user("MEMBER")
    other_member = _make_user("MEMBER")
    group_id = uuid.uuid4()

    # 1. Member's own contribution notification
    own_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=member_user.id,
        notification_type="CONTRIBUTION",
        title="सहयोग प्राप्त हुआ",
        data=json.dumps(
            {"category": "contribution", "amount": 100.0, "target": "member"}
        ),
    )

    # 2. Other member's contribution notification
    other_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=other_member.id,
        notification_type="CONTRIBUTION",
        title="सहयोग प्राप्त हुआ",
        data=json.dumps(
            {"category": "contribution", "amount": 200.0, "target": "member"}
        ),
    )

    # 3. Admin contribution notification (recipient_id is None)
    admin_contrib_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="CONTRIBUTION",
        title="नया सहयोग प्राप्त हुआ",
        data=json.dumps(
            {
                "category": "contribution",
                "amount": 100.0,
                "group_id": str(group_id),
                "target": "admin",
            }
        ),
    )

    # 4. Group payment notification
    group_pay_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="GROUP_PAYMENT",
        title="नया भुगतान प्राप्त हुआ",
        data=json.dumps(
            {
                "category": "group_payment",
                "amount": 5000.0,
                "group_id": str(group_id),
                "target": "admin",
            }
        ),
    )

    # 5. Ad payment notification
    ad_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="AD_PAYMENT",
        title="नया विज्ञापन भुगतान प्राप्त हुआ",
        data=json.dumps(
            {"category": "ad_payment", "amount": 1000.0, "target": "admin"}
        ),
    )

    # Verify access checks
    assert repo.can_user_access(own_notif, member_user) is True
    assert repo.can_user_access(other_notif, member_user) is False
    assert repo.can_user_access(admin_contrib_notif, member_user) is False
    assert repo.can_user_access(group_pay_notif, member_user) is False
    assert repo.can_user_access(ad_notif, member_user) is False

    # Verify SQL filter conditions for member
    filters = repo._apply_filters(current_user=member_user)
    assert len(filters) == 1
    # Filter must enforce recipient_id == member_user.id
    compiled_filter = str(filters[0].compile(compile_kwargs={"literal_binds": True}))
    assert (
        member_user.id.hex in compiled_filter or str(member_user.id) in compiled_filter
    )
    assert "IS NULL" not in compiled_filter


def test_group_admin_notification_access():
    """Group Admin can see fee payments and member contributions for their OWN group,
    and direct personal notifications, but NOT other groups or advertisements.
    """
    session = MagicMock()
    repo = NotificationRepository(session)

    group_a_id = uuid.uuid4()
    group_b_id = uuid.uuid4()

    group_admin_a = _make_user("GROUP_ADMIN", group_id=group_a_id)

    # 1. Group A member contribution (Admin view)
    group_a_contrib = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="CONTRIBUTION",
        title="नया सहयोग प्राप्त हुआ",
        data=json.dumps(
            {
                "category": "contribution",
                "amount": 500.0,
                "group_id": str(group_a_id),
                "target": "admin",
            }
        ),
    )

    # 2. Group A fee payment (Admin view)
    group_a_fee = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="GROUP_PAYMENT",
        title="नया भुगतान प्राप्त हुआ",
        data=json.dumps(
            {
                "category": "group_payment",
                "amount": 10000.0,
                "group_id": str(group_a_id),
                "target": "admin",
            }
        ),
    )

    # 3. Group B member contribution
    group_b_contrib = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="CONTRIBUTION",
        title="नया सहयोग प्राप्त हुआ",
        data=json.dumps(
            {
                "category": "contribution",
                "amount": 300.0,
                "group_id": str(group_b_id),
                "target": "admin",
            }
        ),
    )

    # 4. Group B fee payment
    group_b_fee = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="GROUP_PAYMENT",
        title="नया भुगतान प्राप्त हुआ",
        data=json.dumps(
            {
                "category": "group_payment",
                "amount": 8000.0,
                "group_id": str(group_b_id),
                "target": "admin",
            }
        ),
    )

    # 5. Ad payment notification
    ad_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="AD_PAYMENT",
        title="नया विज्ञापन भुगतान प्राप्त हुआ",
        data=json.dumps(
            {"category": "ad_payment", "amount": 1500.0, "target": "admin"}
        ),
    )

    # 6. Direct notification to Group Admin A
    direct_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=group_admin_a.id,
        notification_type="GENERAL",
        title="Direct Notice",
        data=json.dumps({"category": "general"}),
    )

    # Access checks for Group Admin A
    assert (
        repo.can_user_access(group_a_contrib, group_admin_a, group_ids=[group_a_id])
        is True
    )
    assert (
        repo.can_user_access(group_a_fee, group_admin_a, group_ids=[group_a_id]) is True
    )
    assert (
        repo.can_user_access(direct_notif, group_admin_a, group_ids=[group_a_id])
        is True
    )

    # Must NOT access Group B notifications
    assert (
        repo.can_user_access(group_b_contrib, group_admin_a, group_ids=[group_a_id])
        is False
    )
    assert (
        repo.can_user_access(group_b_fee, group_admin_a, group_ids=[group_a_id])
        is False
    )

    # Must NOT access advertisements
    assert (
        repo.can_user_access(ad_notif, group_admin_a, group_ids=[group_a_id]) is False
    )

    # Verify SQL filter conditions for Group Admin A
    filters = repo._apply_filters(current_user=group_admin_a, group_ids=[group_a_id])
    compiled_filter = str(filters[0].compile(compile_kwargs={"literal_binds": True}))

    # Must include group A ID
    assert str(group_a_id) in compiled_filter
    # Must NOT include group B ID
    assert str(group_b_id) not in compiled_filter

    # Category filter: 'ad_payment' should return false (0 results) for group admin
    ad_filters = repo._apply_filters(
        current_user=group_admin_a, group_ids=[group_a_id], category="ad_payment"
    )
    compiled_ad_filter = str(
        ad_filters[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "false" in compiled_ad_filter.lower() or "0 = 1" in compiled_ad_filter


def test_super_admin_notification_access():
    """Super Admin sees all notifications: all contributions, fee payments,
    advertisements, broadcasts, and personal notifications.
    """
    session = MagicMock()
    repo = NotificationRepository(session)

    super_admin = _make_user("SUPER_ADMIN")
    group_id = uuid.uuid4()

    # 1. Member contribution (Admin view)
    contrib_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="CONTRIBUTION",
        title="नया सहयोग प्राप्त हुआ",
        data=json.dumps(
            {"category": "contribution", "amount": 500.0, "group_id": str(group_id)}
        ),
    )

    # 2. Group fee payment
    fee_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="GROUP_PAYMENT",
        title="नया भुगतान प्राप्त हुआ",
        data=json.dumps(
            {"category": "group_payment", "amount": 10000.0, "group_id": str(group_id)}
        ),
    )

    # 3. Ad payment
    ad_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="AD_PAYMENT",
        title="नया विज्ञापन भुगतान प्राप्त हुआ",
        data=json.dumps({"category": "ad_payment", "amount": 2500.0}),
    )

    # 4. Direct notification to Super Admin
    direct_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=super_admin.id,
        notification_type="GENERAL",
        title="Super Admin Direct",
    )

    assert repo.can_user_access(contrib_notif, super_admin) is True
    assert repo.can_user_access(fee_notif, super_admin) is True
    assert repo.can_user_access(ad_notif, super_admin) is True
    assert repo.can_user_access(direct_notif, super_admin) is True

    # SQL filters for Super Admin
    filters = repo._apply_filters(current_user=super_admin)
    compiled = str(filters[0].compile(compile_kwargs={"literal_binds": True}))
    assert "notifications.recipient_id IS NULL" in compiled
    assert super_admin.id.hex in compiled or str(super_admin.id) in compiled


def test_unauthenticated_user_access():
    """Unauthenticated users (None) cannot access any notifications."""
    session = MagicMock()
    repo = NotificationRepository(session)

    filters = repo._apply_filters(current_user=None, user_id=None)
    compiled = str(filters[0].compile(compile_kwargs={"literal_binds": True}))
    assert "false" in compiled.lower() or "0 = 1" in compiled


@pytest.mark.asyncio
async def test_ad_payment_push_notification_strictly_super_admin():
    """When category='ad_payment', admin push notifications must be targeted ONLY to Super Admins,
    not to Group Admins.
    """
    session = AsyncMock()
    session.add = MagicMock()
    mock_exec_res = MagicMock()
    mock_exec_res.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_exec_res
    super_admin_id = uuid.uuid4()
    advertiser_id = uuid.uuid4()

    with (
        patch(
            "app.services.notification_service.get_admin_user_ids",
            new_callable=AsyncMock,
        ) as mock_get_admins,
        patch(
            "app.services.notification_service.send_push_notification_to_user",
            new_callable=AsyncMock,
        ) as mock_user_push,
        patch(
            "app.services.notification_service.send_push_notification_to_users",
            new_callable=AsyncMock,
        ) as mock_admin_push,
    ):
        mock_get_admins.return_value = [super_admin_id]
        mock_user_push.return_value = 1
        mock_admin_push.return_value = 1

        notif = await create_payment_notification(
            session,
            category="ad_payment",
            amount=Decimal("1500.00"),
            purpose="Banner Ad",
            receipt_no="pay_ad_12345",
            recipient_id=advertiser_id,
        )

        assert notif is not None
        # Verify get_admin_user_ids was called with group_id=None (Super Admins only)
        mock_get_admins.assert_called_once_with(session, group_id=None)
        mock_admin_push.assert_called_once()
        admin_call_args = mock_admin_push.call_args
        assert admin_call_args[0][1] == [super_admin_id]
        assert admin_call_args[1]["title"] == "नया विज्ञापन भुगतान प्राप्त हुआ"


@pytest.mark.asyncio
async def test_endpoint_list_notifications_unauthenticated():
    from app.api.v1.notifications import list_notifications

    db = AsyncMock()
    response = await list_notifications(db=db, current_user=None)
    assert response["success"] is True
    assert response["pagination"]["total"] == 0
    assert response["data"] == []


@pytest.mark.asyncio
async def test_endpoint_get_notification_scoping():
    from datetime import UTC, datetime

    from app.api.v1.notifications import get_notification
    from app.core.exceptions import NotFoundException

    db = AsyncMock()
    # Mock queries inside get_group_admin_group_ids
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = []
    mock_res.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_res

    group_a_id = uuid.uuid4()
    group_b_id = uuid.uuid4()

    member_user = _make_user("MEMBER")
    group_admin_a = _make_user("GROUP_ADMIN", group_id=group_a_id)

    now = datetime.now(UTC)

    # Group B notification (Admin view)
    group_b_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="CONTRIBUTION",
        title="नया सहयोग प्राप्त हुआ",
        is_read=False,
        created_at=now,
        updated_at=now,
        data=json.dumps(
            {"category": "contribution", "amount": 300.0, "group_id": str(group_b_id)}
        ),
    )

    # 1. Member tries to view group B notification -> 404
    with patch.object(
        NotificationRepository, "get_by_id", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = group_b_notif
        with pytest.raises(NotFoundException):
            await get_notification(group_b_notif.id, current_user=member_user, db=db)

    # 2. Group Admin A tries to view group B notification -> 404
    with patch.object(
        NotificationRepository, "get_by_id", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = group_b_notif
        with pytest.raises(NotFoundException):
            await get_notification(group_b_notif.id, current_user=group_admin_a, db=db)

    # 3. Group Admin A views group A notification -> Success
    group_a_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="GROUP_PAYMENT",
        title="नया भुगतान प्राप्त हुआ",
        is_read=False,
        created_at=now,
        updated_at=now,
        data=json.dumps(
            {"category": "group_payment", "amount": 5000.0, "group_id": str(group_a_id)}
        ),
    )
    with patch.object(
        NotificationRepository, "get_by_id", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = group_a_notif
        res = await get_notification(
            group_a_notif.id, current_user=group_admin_a, db=db
        )
        assert res["success"] is True
        assert res["data"].id == group_a_notif.id


@pytest.mark.asyncio
async def test_endpoint_mark_read_and_read_all_scoping():
    from datetime import UTC, datetime

    from app.api.v1.notifications import mark_all_read, mark_notification_read
    from app.core.exceptions import NotFoundException

    db = AsyncMock()
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = []
    mock_res.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_res

    member_user = _make_user("MEMBER")
    now = datetime.now(UTC)
    admin_notif = Notification(
        id=uuid.uuid4(),
        recipient_id=None,
        notification_type="CONTRIBUTION",
        title="Admin Alert",
        is_read=False,
        created_at=now,
        updated_at=now,
    )

    # Member cannot mark admin notification as read -> 404
    with patch.object(
        NotificationRepository, "get_by_id", new_callable=AsyncMock
    ) as mock_get:
        mock_get.return_value = admin_notif
        with pytest.raises(NotFoundException):
            await mark_notification_read(
                admin_notif.id, current_user=member_user, db=db
            )

    # Member read-all should only mark their own notifications
    read_all_res = await mark_all_read(current_user=member_user, db=db)
    assert read_all_res["success"] is True

    # Verify query executed filters by member's id
    assert db.execute.call_count >= 1
    call_args = db.execute.call_args[0][0]
    compiled_stmt = str(call_args.compile(compile_kwargs={"literal_binds": True}))
    assert member_user.id.hex in compiled_stmt or str(member_user.id) in compiled_stmt
    assert "recipient_id IS NULL" not in compiled_stmt
