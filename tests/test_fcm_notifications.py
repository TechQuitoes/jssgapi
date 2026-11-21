from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.firebase import (
    send_fcm_multicast,
    send_fcm_multicast_async,
    send_fcm_push,
    send_fcm_push_async,
)
from app.models.notification import DeviceToken, Notification
from app.services.notification_service import (
    broadcast_push_notification,
    create_payment_notification,
    get_active_device_tokens,
    get_admin_user_ids,
    send_push_notification_to_user,
    send_push_notification_to_users,
)


def test_send_fcm_push_empty_token():
    assert send_fcm_push("", "Title", "Body") is False
    assert send_fcm_push("   ", "Title", "Body") is False


@patch("app.integrations.firebase.firebase_configured", return_value=False)
def test_send_fcm_push_firebase_not_configured(mock_conf):
    assert send_fcm_push("valid_token_123", "Title", "Body") is False


@patch("app.integrations.firebase._get_app")
@patch("firebase_admin.messaging.send")
@patch("app.integrations.firebase.firebase_configured", return_value=True)
def test_send_fcm_push_success(mock_conf, mock_send, mock_get_app):
    mock_app = MagicMock()
    mock_get_app.return_value = mock_app
    mock_send.return_value = "projects/jssg-7e2b0/messages/12345"

    result = send_fcm_push(
        device_token="device_token_xyz",
        title="Contribution Received",
        body="Contribution of ₹251 received from Shreeram Yadav",
        data={
            "category": "contribution",
            "amount": "251.0",
            "donor_name": "Shreeram Yadav",
            "group_name": "JSSG Federation",
            "receipt_no": "order_U2iQqeRSy5kK1A",
            "order_id": "order_123",
            "notification_type": "CONTRIBUTION",
        },
    )

    assert result is True
    assert mock_send.call_count == 1
    sent_msg = mock_send.call_args[0][0]
    assert sent_msg.token == "device_token_xyz"
    assert sent_msg.notification.title == "Contribution Received"
    assert sent_msg.notification.body == "Contribution of ₹251 received from Shreeram Yadav"
    assert sent_msg.android.priority == "high"
    assert sent_msg.android.notification.channel_id == "high_importance_channel"
    assert sent_msg.data["amount"] == "251.0"
    assert sent_msg.data["donor_name"] == "Shreeram Yadav"
    assert sent_msg.data["notification_type"] == "CONTRIBUTION"


@patch("app.integrations.firebase._get_app")
@patch("firebase_admin.messaging.send")
@patch("app.integrations.firebase.firebase_configured", return_value=True)
def test_send_fcm_push_failure_does_not_raise(mock_conf, mock_send, mock_get_app):
    mock_app = MagicMock()
    mock_get_app.return_value = mock_app
    mock_send.side_effect = Exception("Firebase network error")

    result = send_fcm_push(
        device_token="device_token_xyz",
        title="Test",
        body="Body",
    )
    assert result is False


@patch("app.integrations.firebase._get_app")
@patch("firebase_admin.messaging.send_each_for_multicast")
@patch("app.integrations.firebase.firebase_configured", return_value=True)
def test_send_fcm_multicast(mock_conf, mock_send_multicast, mock_get_app):
    mock_app = MagicMock()
    mock_get_app.return_value = mock_app

    mock_batch = MagicMock()
    mock_batch.success_count = 3
    mock_batch.failure_count = 0
    mock_send_multicast.return_value = mock_batch

    tokens = ["token_1", "token_2", "token_3"]
    sent = send_fcm_multicast(
        tokens,
        title="All Users Broadcast",
        body="General Announcement",
        data={"category": "general"},
    )

    assert sent == 3
    assert mock_send_multicast.call_count == 1
    call_msg = mock_send_multicast.call_args[0][0]
    assert call_msg.tokens == tokens
    assert call_msg.notification.title == "All Users Broadcast"
    assert call_msg.android.priority == "high"


@pytest.mark.asyncio
@patch("app.integrations.firebase.send_fcm_push")
async def test_send_fcm_push_async(mock_send):
    mock_send.return_value = True
    result = await send_fcm_push_async("token_123", "Title", "Body", {"key": "val"})
    assert result is True
    mock_send.assert_called_once_with("token_123", "Title", "Body", {"key": "val"})


@pytest.mark.asyncio
async def test_send_push_notification_to_user():
    session = AsyncMock()
    user_id = uuid.uuid4()

    token1 = DeviceToken(user_id=user_id, token="token_alpha", platform="android", is_active=True)
    token2 = DeviceToken(user_id=user_id, token="token_beta", platform="android", is_active=True)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [token1, token2]
    session.execute.return_value = mock_result

    with patch("app.integrations.firebase.send_fcm_push_async", new_callable=AsyncMock) as mock_push:
        mock_push.return_value = True

        delivered = await send_push_notification_to_user(
            session,
            user_id,
            title="Contribution Received",
            body="Contribution of ₹501 received",
            data={"category": "contribution"},
        )

        assert delivered == 2
        assert mock_push.call_count == 2


@pytest.mark.asyncio
async def test_broadcast_push_notification():
    session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = ["token_1", "token_2", "token_3"]
    session.execute.return_value = mock_result

    with patch("app.integrations.firebase.send_fcm_multicast_async", new_callable=AsyncMock) as mock_multi:
        mock_multi.return_value = 3

        count = await broadcast_push_notification(
            session,
            title="General Announcement",
            body="Event tomorrow",
            data={"category": "event"},
        )

        assert count == 3
        mock_multi.assert_called_once_with(
            ["token_1", "token_2", "token_3"],
            "General Announcement",
            "Event tomorrow",
            {"category": "event"},
        )


@pytest.mark.asyncio
async def test_create_payment_notification_member_and_admin_alerts():
    """Verify that on payment/contribution:
    a) Member receives confirmation: 'आपका ₹X का सहयोग सफलतापूर्वक प्राप्त हुआ, धन्यवाद!'
    b) Group Admins & Super Admins receive alert: '₹X का नया सहयोग प्राप्त हुआ - {donor_name}'
    """
    session = AsyncMock()
    session.add = MagicMock()
    member_user_id = uuid.uuid4()
    group_admin_id = uuid.uuid4()
    super_admin_id = uuid.uuid4()
    group_id = uuid.uuid4()

    with patch("app.services.notification_service.get_admin_user_ids", new_callable=AsyncMock) as mock_get_admins, \
         patch("app.services.notification_service.send_push_notification_to_user", new_callable=AsyncMock) as mock_member_push, \
         patch("app.services.notification_service.send_push_notification_to_users", new_callable=AsyncMock) as mock_admin_push:

        mock_get_admins.return_value = [super_admin_id, group_admin_id]
        mock_member_push.return_value = 1
        mock_admin_push.return_value = 2

        notif = await create_payment_notification(
            session,
            category="contribution",
            amount=Decimal("501.00"),
            group_id=group_id,
            donor_name="Pankaj Jain",
            group_name="Indore Mandal",
            purpose="Mandir Seva",
            receipt_no="pay_contrib_45678",
            order_id="order_contrib_98765",
            recipient_id=member_user_id,
        )

        assert notif is not None
        # Verify 2 notifications were added to session: one for member, one for admin view
        assert session.add.call_count == 2
        assert session.flush.call_count >= 1

        # a) Verify Member Push
        mock_member_push.assert_called_once()
        member_call = mock_member_push.call_args
        assert member_call[0][1] == member_user_id
        assert member_call[1]["title"] == "सहयोग प्राप्त हुआ"
        assert "आपका ₹501 का सहयोग सफलतापूर्वक प्राप्त हुआ, धन्यवाद!" in member_call[1]["body"]
        assert member_call[1]["data"]["target"] == "member"

        # b) Verify Group Admin & Super Admin Push
        mock_admin_push.assert_called_once()
        admin_call = mock_admin_push.call_args
        target_ids = admin_call[0][1]
        assert set(target_ids) == {super_admin_id, group_admin_id}
        assert admin_call[1]["title"] == "नया सहयोग प्राप्त हुआ"
        assert "₹501 का नया सहयोग प्राप्त हुआ - Pankaj Jain (Indore Mandal)" in admin_call[1]["body"]
        assert admin_call[1]["data"]["target"] == "admin"
