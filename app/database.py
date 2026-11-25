from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

settings = get_settings()

# Safe startup diagnostics -- never logs the URL, hostname, or credentials.
_split = urlsplit(settings.database_url)
_host = (_split.hostname or "").lower()

_looks_unreachable = (
    not _host
    or _host in {
        "localhost",
        "127.0.0.1",
        "::1",
        "db",
        "postgres",
        "jssg-db",
    }
    or _host.endswith((".internal", ".local"))
)

logging.getLogger(__name__).warning(
    "database config: env_var_set=%s scheme=%s driver=asyncpg "
    "hostname_present=%s database_present=%s looks_unreachable=%s",
    bool(os.environ.get("DATABASE_URL")),
    _split.scheme,
    bool(_host),
    bool(_split.path and _split.path.strip("/")),
    _looks_unreachable,
)

_connect_args: dict[str, Any] = {
    "statement_cache_size": 0,
}
if not _looks_unreachable:
    _connect_args["ssl"] = "require"

engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    connect_args=_connect_args,
    pool_size=5,
    max_overflow=5,
    pool_recycle=300,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            request.state.db = session
        except Exception:
            pass
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
