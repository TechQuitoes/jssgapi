#!/usr/bin/env python3
"""Script to set password of all GROUP_ADMIN users to Pass@1234.

Usage:
    python -m scripts.set_group_admin_passwords
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.core.constants import DEFAULT_USER_PASSWORD, RoleName
from app.core.security import hash_password
from app.database import async_session_factory
from app.models.user import Role, User


async def run() -> None:
    password_hash = hash_password(DEFAULT_USER_PASSWORD)
    print(f"Target password: {DEFAULT_USER_PASSWORD}")

    async with async_session_factory() as session:
        role_res = await session.execute(
            select(Role).where(Role.name == RoleName.GROUP_ADMIN.value)
        )
        role = role_res.scalar_one_or_none()
        if not role:
            print("GROUP_ADMIN role not found!")
            return

        users_res = await session.execute(
            select(User).where(User.role_id == role.id)
        )
        admins = list(users_res.scalars().all())
        print(f"Found {len(admins)} group admins.")

        for admin in admins:
            admin.password_hash = password_hash

        await session.commit()
        print(f"Successfully updated password to '{DEFAULT_USER_PASSWORD}' for all {len(admins)} group admins.")


if __name__ == "__main__":
    asyncio.run(run())
