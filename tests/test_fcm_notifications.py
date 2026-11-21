from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.firebase import send_fcm_push, send_fcm_push_async
from app.models.notification import DeviceToken, Notification
from app.services.notification_service import (
    create_payment_notification,
    get_active_device_tokens,
    send_push_notification_to_user,
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
async def test_create_payment_notification_triggers_push():
    session = AsyncMock()
    session.add = MagicMock()
    recipient_id = uuid.uuid4()

    with patch("app.services.notification_service.send_push_notification_to_user", new_callable=AsyncMock) as mock_push:
        mock_push.return_value = 1

        notif = await create_payment_notification(
            session,
            category="contribution",
            amount=Decimal("251.00"),
            donor_name="Shreeram Yadav",
            group_name="JSSG Federation",
            purpose="Temple Fund",
            receipt_no="pay_12345",
            order_id="order_12345",
            recipient_id=recipient_id,
        )

        assert notif is not None
        assert notif.title == "Contribution Received"
        assert notif.notification_type == "CONTRIBUTION"
        session.add.assert_called_once()
        session.flush.assert_called_once()

        mock_push.assert_called_once()
        call_args = mock_push.call_args
        assert call_args[0][1] == recipient_id
        assert call_args[1]["title"] == "Contribution Received"
        assert "₹251" in call_args[1]["body"]
        assert "Shreeram Yadav" in call_args[1]["body"]
        assert call_args[1]["data"]["category"] == "contribution"
        assert call_args[1]["data"]["amount"] == "251.00"
        assert call_args[1]["data"]["donor_name"] == "Shreeram Yadav"
        assert call_args[1]["data"]["group_name"] == "JSSG Federation"
        assert call_args[1]["data"]["order_id"] == "order_12345"
