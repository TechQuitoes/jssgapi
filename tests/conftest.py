"""Test session bootstrap.

The suite used to run straight against the database named in ``.env`` (the local
development database) because every test imports the module level
``app.database.engine``, and nothing ever redirected it. A single run therefore
wrote fixtures, seeded rows and synced records into real development data.

This module makes that impossible:

* the target database is always a dedicated throwaway database whose name ends in
  ``_test``;
* the schema is built from the Alembic migrations, then roles/permissions are
  seeded the same way the application seeds them at start up;
* the database is dropped again when the session ends (set ``KEEP_TEST_DB=1`` to
  keep it for debugging).

``DATABASE_URL`` is exported before any ``app`` module is imported, which is what
matters: ``app.database`` builds its engine at import time from the settings, and
``pydantic-settings`` gives environment variables precedence over ``.env``.

The database is dropped *and recreated* at the start of every session as well as
at the end: a run that was killed halfway (timeout, Ctrl-C) can never execute the
end-of-session drop, and stale rows from it would otherwise break tests that
assume empty tables on the next run.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DB_NAME = os.environ.get("JSSG_TEST_DB_NAME", "jssg_test")
REQUIRED_DB_SUFFIX = "_test"
MAINTENANCE_DB = "postgres"


def _database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def _configured_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        from dotenv import dotenv_values

        url = dotenv_values(ROOT / ".env").get("DATABASE_URL")
    if not url:
        url = "postgresql+asyncpg://jssg:jssg_secret@localhost:5432/jssg_db"
    return url


def _resolve_test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    url = explicit or _with_database(_configured_database_url(), TEST_DB_NAME)

    name = _database_name(url)
    if not name.endswith(REQUIRED_DB_SUFFIX):
        raise RuntimeError(
            f"Refusing to run the test suite against database {name!r}. Test "
            f"databases must end in {REQUIRED_DB_SUFFIX!r} so that a stray run can "
            f"never touch development or production data. Set TEST_DATABASE_URL to "
            f"a dedicated throwaway database, for example "
            f"postgresql+asyncpg://user:pass@localhost:5432/jssg_test."
        )
    return url


def _asyncpg_dsn(url: str, database: str) -> str:
    """Strip the SQLAlchemy driver marker so asyncpg can open the connection."""
    parts = urlsplit(url)
    return urlunsplit(
        parts._replace(
            scheme=parts.scheme.split("+", 1)[0],
            path=f"/{database}",
            query=parts.query,
            fragment="",
        )
    )


TEST_DATABASE_URL = _resolve_test_database_url()
TEST_DATABASE_NAME = _database_name(TEST_DATABASE_URL)

# Must happen before the first ``app`` import so the engine binds to the test DB.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


async def _create_test_database() -> None:
    import asyncpg

    connection = await asyncpg.connect(_asyncpg_dsn(TEST_DATABASE_URL, MAINTENANCE_DB))
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DATABASE_NAME
        )
        if exists:
            # A killed run (timeout, Ctrl-C) never reaches the end-of-session
            # drop, and its leftover rows silently break tests that assume an
            # empty table. Every session therefore starts from a clean slate.
            await connection.execute(
                f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}" WITH (FORCE)'
            )
        try:
            await connection.execute(f'CREATE DATABASE "{TEST_DATABASE_NAME}"')
        except asyncpg.InsufficientPrivilegeError as exc:
            role = urlsplit(TEST_DATABASE_URL).username or "jssg"
            raise RuntimeError(
                f"Cannot create the test database {TEST_DATABASE_NAME!r}: the role "
                f"{role!r} lacks CREATEDB. Grant it once as a superuser with "
                f"'ALTER ROLE {role} CREATEDB;', or pre-create the database with "
                f"'CREATE DATABASE {TEST_DATABASE_NAME} OWNER {role};'."
            ) from exc
    finally:
        await connection.close()


async def _drop_test_database() -> None:
    import asyncpg

    connection = await asyncpg.connect(_asyncpg_dsn(TEST_DATABASE_URL, MAINTENANCE_DB))
    try:
        await connection.execute(
            f'DROP DATABASE IF EXISTS "{TEST_DATABASE_NAME}" WITH (FORCE)'
        )
    finally:
        await connection.close()


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database():
    """Create, migrate and seed a throwaway database for the whole session."""
    from app.database import engine

    if not (engine.url.database or "").endswith(REQUIRED_DB_SUFFIX):
        raise RuntimeError(
            f"app.database.engine points at {engine.url.database!r}; the suite must "
            f"only ever talk to a database ending in {REQUIRED_DB_SUFFIX!r}."
        )

    if not os.environ.get("KEEP_TEST_DB"):
        asyncio.run(_create_test_database())

        from alembic import command
        from alembic.config import Config

        alembic_config = Config(str(ROOT / "alembic.ini"))
        alembic_config.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(alembic_config, "head")

        from app.database import async_session_factory
        from app.services.bootstrap_service import bootstrap_app

        # Roles and permissions live outside the migrations, so seed them the way the
        # application does at start up.
        asyncio.run(bootstrap_app(async_session_factory))
        # The bootstrap ran on its own event loop. Drop those pooled connections so
        # the app (driven by TestClient's portal loop) does not reuse a connection
        # bound to a closed loop.
        asyncio.run(engine.dispose())

    try:
        yield
    finally:
        asyncio.run(engine.dispose())
        if not os.environ.get("KEEP_TEST_DB"):
            asyncio.run(_drop_test_database())
