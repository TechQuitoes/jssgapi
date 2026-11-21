from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials, messaging

from app.config import FIREBASE_PROJECT_ID, get_settings
from app.core.exceptions import UnauthorizedException

logger = logging.getLogger(__name__)

_APP: firebase_admin.App | None = None
_APP_LOCK = threading.Lock()
_APP_NAME = "jssg-auth"


def firebase_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.firebase_project_id == FIREBASE_PROJECT_ID
        and settings.firebase_private_key
        and settings.firebase_client_email
    )


def _private_key_pem(value: str) -> str:
    key = value.replace("\\n", "\n").strip()
    if "-----BEGIN" in key:
        return key
    return f"-----BEGIN PRIVATE KEY-----\n{key}\n-----END PRIVATE KEY-----"


def _get_app() -> firebase_admin.App:
    global _APP
    if _APP is not None:
        return _APP
    if not firebase_configured():
        raise UnauthorizedException("Firebase is not configured on the server")
    with _APP_LOCK:
        if _APP is not None:
            return _APP
        settings = get_settings()
        cert = credentials.Certificate(
            {
                "type": "service_account",
                "project_id": FIREBASE_PROJECT_ID,
                "private_key": _private_key_pem(settings.firebase_private_key),
                "client_email": settings.firebase_client_email,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
        try:
            app = firebase_admin.get_app(_APP_NAME)
        except ValueError:
            app = firebase_admin.initialize_app(
                cert,
                options={"projectId": FIREBASE_PROJECT_ID},
                name=_APP_NAME,
            )
        if app.project_id != FIREBASE_PROJECT_ID:
            firebase_admin.delete_app(_APP_NAME)
            app = firebase_admin.initialize_app(
                cert,
                options={"projectId": FIREBASE_PROJECT_ID},
                name=_APP_NAME,
            )
        # Also ensure default app is initialized so any direct messaging calls work
        if firebase_admin._DEFAULT_APP_NAME not in firebase_admin._apps:
            try:
                firebase_admin.initialize_app(
                    cert,
                    options={"projectId": FIREBASE_PROJECT_ID},
                )
            except ValueError:
                pass
        _APP = app
        return _APP


def get_firebase_app() -> firebase_admin.App:
    """Public accessor to obtain the initialized Firebase App instance."""
    return _get_app()


def verify_id_token(id_token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return its claims.

    Only the Admin SDK's decoded claims are ever trusted. Any invalid,
    expired, or malformed token raises ``UnauthorizedException``.
    """
    try:
        return firebase_auth.verify_id_token(id_token, app=_get_app())
    except UnauthorizedException:
        raise
    except Exception:
        raise UnauthorizedException("Invalid or expired Firebase ID token") from None


def send_fcm_push(
    device_token: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> bool:
    """Sends an FCM push notification to a specific device token.

    Works when app is closed, in background, or in foreground.
    Never raises an exception so primary workflows are never disrupted.
    """
    if not device_token or not device_token.strip():
        logger.warning("FCM Push skipped: empty device_token")
        return False

    if not firebase_configured():
        logger.warning("FCM Push skipped: Firebase is not configured on the server")
        return False

    try:
        app = _get_app()
    except Exception as exc:
        logger.error("FCM Push failed: unable to get Firebase app: %s", exc)
        return False

    try:
        # Convert all data dictionary values to string (FCM requirement)
        string_data = {
            str(k): str(v)
            for k, v in (data or {}).items()
            if v is not None
        }

        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=string_data,
            token=device_token,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="high_importance_channel",
                    sound="default",
                    default_sound=True,
                    default_vibrate_timings=True,
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        badge=1,
                    )
                )
            ),
        )
        response = messaging.send(message, app=app)
        logger.info("FCM Push sent successfully: %s", response)
        return True
    except messaging.UnregisteredError:
        logger.warning(
            "FCM Push failed: device token is unregistered or expired: %s...",
            (device_token or "")[:15],
        )
        return False
    except Exception as e:
        logger.error(
            "FCM Push failed for token %s...: %s",
            (device_token or "")[:15],
            e,
        )
        return False


async def send_fcm_push_async(
    device_token: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> bool:
    """Asynchronously sends an FCM push notification without blocking the asyncio loop."""
    return await asyncio.to_thread(send_fcm_push, device_token, title, body, data)


def send_fcm_multicast(
    device_tokens: list[str],
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Sends an FCM push notification to multiple device tokens via multicast.

    Batches tokens into chunks of 500 (Firebase Admin maximum).
    Catches all errors so caller flows are never disrupted. Returns total success count.
    """
    valid_tokens = [t.strip() for t in device_tokens if t and t.strip()]
    if not valid_tokens:
        logger.warning("FCM Multicast skipped: no valid device tokens provided")
        return 0

    if not firebase_configured():
        logger.warning("FCM Multicast skipped: Firebase is not configured on the server")
        return 0

    try:
        app = _get_app()
    except Exception as exc:
        logger.error("FCM Multicast failed: unable to get Firebase app: %s", exc)
        return 0

    try:
        from firebase_admin import messaging

        string_data = {
            str(k): str(v)
            for k, v in (data or {}).items()
            if v is not None
        }

        notification = messaging.Notification(title=title, body=body)
        android_config = messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="high_importance_channel",
                sound="default",
                default_sound=True,
                default_vibrate_timings=True,
            ),
        )
        apns_config = messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(sound="default", badge=1)
            )
        )

        total_success = 0
        chunk_size = 500
        for i in range(0, len(valid_tokens), chunk_size):
            chunk = valid_tokens[i : i + chunk_size]
            try:
                multicast_message = messaging.MulticastMessage(
                    tokens=chunk,
                    notification=notification,
                    data=string_data,
                    android=android_config,
                    apns=apns_config,
                )
                response = messaging.send_each_for_multicast(multicast_message, app=app)
                total_success += response.success_count
                logger.info(
                    "FCM Multicast chunk sent: %d succeeded, %d failed out of %d tokens",
                    response.success_count,
                    response.failure_count,
                    len(chunk),
                )
            except Exception as chunk_err:
                logger.error("FCM Multicast chunk error: %s", chunk_err)

        return total_success
    except Exception as exc:
        logger.error("FCM Multicast failed: %s", exc)
        return 0


async def send_fcm_multicast_async(
    device_tokens: list[str],
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Asynchronously sends an FCM multicast notification without blocking asyncio event loop."""
    return await asyncio.to_thread(send_fcm_multicast, device_tokens, title, body, data)
