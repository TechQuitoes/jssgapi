from __future__ import annotations

from sqlalchemy import select

from app.models.payment import Payment, PaymentTransaction
from app.repositories.base_repository import BaseRepository


class PaymentRepository(BaseRepository[Payment]):
    model = Payment

    async def get_by_order_id(self, order_id: str) -> Payment | None:
        result = await self.session.execute(
            select(Payment).where(Payment.order_id == order_id)
        )
        return result.scalar_one_or_none()

    async def get_by_payment_link_id(self, payment_link_id: str) -> Payment | None:
        result = await self.session.execute(
            select(Payment).where(Payment.payment_link_id == payment_link_id)
        )
        return result.scalar_one_or_none()

    async def list_for_member(
        self, member_id, limit: int, offset: int
    ) -> list[Payment]:
        result = await self.session.execute(
            select(Payment)
            .where(Payment.member_id == member_id)
            .order_by(Payment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_for_obligation(
        self, obligation_id, limit: int = 100, offset: int = 0
    ) -> list[Payment]:
        result = await self.session.execute(
            select(Payment)
            .where(Payment.obligation_id == obligation_id)
            .order_by(Payment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())


class PaymentTransactionRepository(BaseRepository[PaymentTransaction]):
    model = PaymentTransaction

    async def get_by_razorpay_payment_id(
        self, razorpay_payment_id: str
    ) -> PaymentTransaction | None:
        result = await self.session.execute(
            select(PaymentTransaction).where(
                PaymentTransaction.razorpay_payment_id == razorpay_payment_id
            )
        )
        return result.scalar_one_or_none()

    async def list_for_obligation(
        self, obligation_id, limit: int = 100
    ) -> list[PaymentTransaction]:
        result = await self.session.execute(
            select(PaymentTransaction)
            .where(PaymentTransaction.obligation_id == obligation_id)
            .order_by(PaymentTransaction.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def create_for_payment(
        self,
        payment: Payment,
        transaction_type: str,
        status: str,
        **kwargs,
    ) -> PaymentTransaction:
        return await self.create(
            payment_id=payment.id,
            transaction_type=transaction_type,
            status=status,
            **kwargs,
        )

