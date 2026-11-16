"""Webhook receiver endpoints for external integrations (Razorpay, etc.).

These endpoints receive event callbacks directly from third-party services.
They require no user authentication but verify request signatures using
shared webhook secrets.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.core.responses import ok
from app.dependencies import DBSession
from app.services.payment_service import PaymentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post("/razorpay")
async def razorpay_webhook(request: Request, db: DBSession):
    """Receive Razorpay webhook events (fail-safe for mobile app disconnections).

    Called directly by Razorpay servers:
    - Verifies the ``X-Razorpay-Signature`` header with ``RAZORPAY_WEBHOOK_SECRET``.
    - Handles ``order.paid`` and ``payment.captured`` events.
    - Idempotently updates Payment record to SUCCESS and Fee/Obligation to PAID.
    """
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    service = PaymentService(db)
    result = await service.handle_webhook(body=body, signature=signature)

    logger.info("Razorpay webhook processed via /webhooks/razorpay: %s", result)
    return ok("Webhook processed successfully", result)
