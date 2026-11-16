"""Razorpay payment gateway integration.

Wraps the official ``razorpay`` SDK to expose only the operations the
application needs: **create order**, **verify payment signature**, and
**fetch payment details**.

All amounts are handled in **paise** internally (Razorpay requirement).
The public helpers accept ``Decimal`` rupees and convert automatically.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import Decimal
from typing import Any

import razorpay

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


def _get_razorpay_client() -> razorpay.Client:
    """Return a configured Razorpay SDK client.

    Raises ``RuntimeError`` when credentials are missing so the caller gets a
    clear failure instead of a cryptic auth error from the gateway.
    """
    key_id = settings.razorpay_key_id
    key_secret = settings.razorpay_key_secret
    if not key_id or not key_secret:
        raise RuntimeError(
            "Razorpay credentials are not configured. "
            "Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in the environment."
        )
    return razorpay.Client(auth=(key_id, key_secret))


def _rupees_to_paise(amount: Decimal) -> int:
    """Convert an INR amount to the paise integer Razorpay expects."""
    return int(amount * 100)


# ── Order ────────────────────────────────────────────────────────────────


def create_razorpay_order(
    *,
    amount: Decimal,
    currency: str = "INR",
    receipt: str | None = None,
    notes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a Razorpay order and return the full gateway response.

    Parameters
    ----------
    amount:
        Order amount **in rupees** (e.g. ``Decimal("499.00")``).
    currency:
        ISO currency code – usually ``"INR"``.
    receipt:
        An internal reference string (our ``order_id``).
    notes:
        Free-form key-value pairs attached to the order on Razorpay's side.

    Returns
    -------
    dict
        The Razorpay order object, including at least ``id``, ``amount``,
        ``currency``, ``receipt``, and ``status``.
    """
    client = _get_razorpay_client()
    payload: dict[str, Any] = {
        "amount": _rupees_to_paise(amount),
        "currency": currency,
        "payment_capture": 1,  # auto-capture
    }
    if receipt:
        payload["receipt"] = receipt
    if notes:
        payload["notes"] = notes

    logger.info(
        "Creating Razorpay order: amount=%s %s, receipt=%s",
        amount,
        currency,
        receipt,
    )
    order = client.order.create(data=payload)
    logger.info("Razorpay order created: %s", order.get("id"))
    return order


# ── Signature verification ───────────────────────────────────────────────


def verify_razorpay_payment_signature(
    *,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> bool:
    """Verify the payment signature returned by Razorpay Checkout.

    The client (Flutter app) receives ``razorpay_order_id``,
    ``razorpay_payment_id`` and ``razorpay_signature`` after a successful
    payment. This function re-computes the HMAC-SHA256 digest and compares
    it with the provided signature.

    Returns ``True`` when the signature is valid, ``False`` otherwise.
    """
    key_secret = settings.razorpay_key_secret
    if not key_secret:
        raise RuntimeError("RAZORPAY_KEY_SECRET is not configured.")

    message = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected = hmac.new(
        key_secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected, razorpay_signature)
    if not is_valid:
        logger.warning(
            "Razorpay signature mismatch for order=%s payment=%s",
            razorpay_order_id,
            razorpay_payment_id,
        )
    return is_valid


# ── Fetch payment details ────────────────────────────────────────────────


def fetch_razorpay_payment(payment_id: str) -> dict[str, Any]:
    """Fetch a payment's details from the Razorpay API."""
    client = _get_razorpay_client()
    return client.payment.fetch(payment_id)


# ── Payment Links ────────────────────────────────────────────────────────


def create_razorpay_payment_link(
    *,
    amount: Decimal,
    currency: str = "INR",
    description: str,
    customer: dict[str, str] | None = None,
    notes: dict[str, str] | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    """Create a standard Razorpay Payment Link for hosted payment collection.

    Parameters
    ----------
    amount:
        Amount in rupees.
    currency:
        Currency code, default 'INR'.
    description:
        Purpose of payment.
    customer:
        Dict containing name, email, contact.
    notes:
        Internal tracking metadata (group_id, obligation_id, etc.).
    callback_url:
        Optional redirect after payment.

    Returns
    -------
    dict
        Razorpay Payment Link object including 'id', 'short_url', 'status'.
    """
    client = _get_razorpay_client()
    payload: dict[str, Any] = {
        "amount": _rupees_to_paise(amount),
        "currency": currency,
        "accept_partial": False,
        "description": description,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
    }
    if customer:
        cust_data = {}
        if customer.get("name"):
            cust_data["name"] = customer["name"]
        if customer.get("email"):
            cust_data["email"] = customer["email"]
        if customer.get("contact"):
            cust_data["contact"] = customer["contact"]
        if cust_data:
            payload["customer"] = cust_data
    if notes:
        payload["notes"] = notes
    if callback_url:
        payload["callback_url"] = callback_url
        payload["callback_method"] = "get"

    logger.info("Creating Razorpay Payment Link: amount=%s %s", amount, currency)
    link = client.payment_link.create(data=payload)
    logger.info("Razorpay Payment Link created: id=%s url=%s", link.get("id"), link.get("short_url"))
    return link


def cancel_razorpay_payment_link(payment_link_id: str) -> dict[str, Any] | None:
    """Cancel an active, unpaid Razorpay Payment Link."""
    client = _get_razorpay_client()
    try:
        logger.info("Cancelling Razorpay Payment Link: %s", payment_link_id)
        return client.payment_link.cancel(payment_link_id)
    except Exception as exc:
        logger.warning("Could not cancel Razorpay Payment Link %s: %s", payment_link_id, exc)
        return None


def fetch_razorpay_payment_link(payment_link_id: str) -> dict[str, Any]:
    """Fetch details of a Razorpay Payment Link."""
    client = _get_razorpay_client()
    return client.payment_link.fetch(payment_link_id)


# ── Webhook signature verification ──────────────────────────────────────


def verify_razorpay_webhook_signature(
    body: bytes,
    signature: str,
) -> bool:
    """Verify the ``X-Razorpay-Signature`` header on an incoming webhook.

    Uses ``RAZORPAY_WEBHOOK_SECRET`` from the environment.
    """
    webhook_secret = settings.razorpay_webhook_secret
    if not webhook_secret:
        logger.error("RAZORPAY_WEBHOOK_SECRET is not configured; rejecting webhook.")
        return False

    expected = hmac.new(
        webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)
