from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import redis.asyncio as aioredis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def _sanitize_redis_url(url: str) -> str:
    """Mask credentials in Redis URL for safe logging."""
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = parts.netloc.replace(parts.password, "******")
            if parts.username:
                netloc = netloc.replace(parts.username, "******")
            return urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
            )
    except Exception:
        pass
    return "redis://***:***@host:port"


class RedisManager:
    """Async Redis connection and cache manager with graceful degradation.

    All methods catch connection errors and return None/False so that
    the application falls back to PostgreSQL seamlessly if Redis is down.
    When disconnected, calls back off silently instead of spamming connection errors.
    """

    def __init__(self, reconnect_interval: float = 30.0) -> None:
        self._pool: aioredis.ConnectionPool | None = None
        self._client: aioredis.Redis | None = None
        self._enabled: bool = False
        self._is_connected: bool = False
        self._last_reconnect_attempt: float = 0.0
        self._reconnect_interval: float = reconnect_interval

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def is_connected(self) -> bool:
        return self._enabled and self._is_connected

    async def _check_or_reconnect(self) -> bool:
        """Check if Redis is available. If disconnected, attempt a quiet reconnect at intervals."""
        if not self._enabled or not self._client:
            return False
        if self._is_connected:
            return True

        now = time.monotonic()
        if now - self._last_reconnect_attempt < self._reconnect_interval:
            return False

        self._last_reconnect_attempt = now
        try:
            await self._client.ping()
            self._is_connected = True
            logger.info("Redis connection re-established.")
            return True
        except Exception:
            self._is_connected = False
            return False

    async def init(self, redis_url: str, enabled: bool = True) -> None:
        self._enabled = enabled
        if not self._enabled:
            logger.info("Redis caching is disabled (REDIS_ENABLED=False).")
            self._is_connected = False
            return

        sanitized_url = _sanitize_redis_url(redis_url)
        try:
            self._pool = aioredis.ConnectionPool.from_url(
                redis_url,
                decode_responses=True,
                max_connections=20,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            self._client = aioredis.Redis(connection_pool=self._pool)
            # Verify connectivity
            await self._client.ping()
            self._is_connected = True
            logger.info("Connected to Redis at %s", sanitized_url)
        except Exception as exc:
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            logger.warning(
                "Failed to connect to Redis at %s (%s). "
                "Operating in fallback bypass mode (reconnecting in background).",
                sanitized_url,
                type(exc).__name__,
            )

    async def close(self) -> None:
        self._is_connected = False
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:
                logger.warning("Error closing Redis client: %s", exc)
            self._client = None
        if self._pool is not None:
            try:
                await self._pool.disconnect()
            except Exception as exc:
                logger.warning("Error disconnecting Redis pool: %s", exc)
            self._pool = None

    async def ping(self) -> bool:
        if not self._enabled or not self._client:
            return False
        try:
            ok = bool(await self._client.ping())
            self._is_connected = ok
            return ok
        except Exception as exc:
            logger.warning("Redis ping failed: %s", type(exc).__name__)
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return False

    async def get(self, key: str) -> str | None:
        if not await self._check_or_reconnect():
            return None
        try:
            return await self._client.get(key)
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning(
                "Redis get('%s') failed: %s; switching to fallback mode.",
                key,
                type(exc).__name__,
            )
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return None

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        if not await self._check_or_reconnect():
            return False
        try:
            if ttl is not None and ttl > 0:
                await self._client.set(key, value, ex=ttl)
            else:
                await self._client.set(key, value)
            return True
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning(
                "Redis set('%s') failed: %s; switching to fallback mode.",
                key,
                type(exc).__name__,
            )
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return False

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode JSON from cache key '%s': %s", key, exc)
            return None

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> bool:
        try:
            raw = json.dumps(value, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("Failed to serialize JSON for cache key '%s': %s", key, exc)
            return False
        return await self.set(key, raw, ttl=ttl)

    async def delete(self, *keys: str) -> int:
        if not keys or not await self._check_or_reconnect():
            return 0
        try:
            return await self._client.delete(*keys)
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning(
                "Redis delete failed: %s; switching to fallback mode.",
                type(exc).__name__,
            )
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return 0

    async def delete_pattern(self, pattern: str) -> int:
        """Scan and delete all keys matching pattern in non-blocking batches."""
        if not await self._check_or_reconnect():
            return 0
        count = 0
        batch: list[str] = []
        try:
            async for key in self._client.scan_iter(match=pattern, count=100):
                batch.append(key)
                if len(batch) >= 100:
                    await self._client.delete(*batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                await self._client.delete(*batch)
                count += len(batch)
            return count
        except (RedisError, OSError, TimeoutError) as exc:
            logger.warning(
                "Redis delete_pattern('%s') failed: %s; switching to fallback mode.",
                pattern,
                type(exc).__name__,
            )
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return count


def _sanitize_upstash_url(url: str | None) -> str:
    """Mask host and credentials for safe Upstash logging."""
    if not url:
        return "<not-configured>"
    try:
        parts = urlsplit(url)
        host = parts.netloc.split("@")[-1]
        return f"{parts.scheme}://{host}"
    except Exception:
        return "https://***.upstash.io"


class UpstashRedisManager:
    """Asynchronous Upstash Redis client using REST API for serverless environments.

    Designed for Vercel functions where persistent TCP connections are not ideal.
    Uses stateless HTTPS via httpx.AsyncClient with reasonable timeouts (3.0s).
    Catches all HTTP errors without swallowing asyncio.CancelledError and returns
    fallback defaults (None, False, 0) so PostgreSQL directly serves data.
    When disconnected, calls back off silently instead of spamming connection errors.
    """

    def __init__(self, timeout: float = 3.0, reconnect_interval: float = 30.0) -> None:
        self._url: str | None = None
        self._token: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._enabled: bool = False
        self._is_connected: bool = False
        self._timeout: float = timeout
        self._last_reconnect_attempt: float = 0.0
        self._reconnect_interval: float = reconnect_interval

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def is_connected(self) -> bool:
        return self._enabled and self._is_connected

    async def _check_or_reconnect(self) -> bool:
        """Check if Upstash is available. If disconnected, attempt a quiet reconnect at intervals."""
        if not self._enabled or not self._client:
            return False
        if self._is_connected:
            return True

        now = time.monotonic()
        if now - self._last_reconnect_attempt < self._reconnect_interval:
            return False

        self._last_reconnect_attempt = now
        try:
            ok = await self.ping()
            if ok:
                self._is_connected = True
                logger.info("Upstash Redis connection re-established.")
                return True
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def init(
        self,
        url: str | None,
        token: str | None,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        if not self._enabled:
            logger.info("Upstash Redis caching is disabled (REDIS_ENABLED=False).")
            self._is_connected = False
            return

        if not url or not token:
            self._is_connected = False
            logger.warning(
                "Upstash Redis credentials missing (UPSTASH_REDIS_REST_URL/TOKEN). "
                "Operating in fallback bypass mode."
            )
            return

        self._url = url.rstrip("/")
        self._token = token.strip()
        sanitized = _sanitize_upstash_url(self._url)
        try:
            self._client = httpx.AsyncClient(
                base_url=self._url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(self._timeout, connect=2.0),
            )
            # Verify connectivity via PING
            ok = await self.ping()
            if ok:
                self._is_connected = True
                logger.info("Connected to Upstash Redis at %s", sanitized)
            else:
                self._is_connected = False
                self._last_reconnect_attempt = time.monotonic()
                logger.warning(
                    "Failed to verify Upstash Redis connectivity at %s. "
                    "Operating in fallback bypass mode.",
                    sanitized,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            logger.warning(
                "Upstash Redis init failed at %s (%s). Operating in fallback bypass mode.",
                sanitized,
                type(exc).__name__,
            )

    async def close(self) -> None:
        self._is_connected = False
        if self._client is not None:
            try:
                await self._client.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Error closing Upstash client: %s", type(exc).__name__)
            self._client = None

    async def ping(self) -> bool:
        if not self._enabled or not self._client:
            return False
        try:
            resp = await self._client.post("/", json=["PING"])
            if resp.status_code == 200:
                data = resp.json()
                ok = data.get("result") in ("PONG", True, "OK")
                self._is_connected = ok
                return ok
            self._is_connected = False
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Upstash ping failed: %s", type(exc).__name__)
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return False

    async def get(self, key: str) -> str | None:
        if not await self._check_or_reconnect():
            return None
        try:
            resp = await self._client.post("/", json=["GET", key])
            if resp.status_code == 200:
                res = resp.json().get("result")
                if res is not None:
                    self._is_connected = True
                    return str(res)
                return None
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Upstash get('%s') failed: %s; switching to fallback mode.", key, type(exc).__name__)
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return None

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        if not await self._check_or_reconnect():
            return False
        try:
            cmd = ["SET", key, value]
            if ttl is not None and ttl > 0:
                cmd.extend(["EX", ttl])
            resp = await self._client.post("/", json=cmd)
            if resp.status_code == 200:
                data = resp.json()
                ok = data.get("result") in ("OK", True, 1)
                if ok:
                    self._is_connected = True
                return ok
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Upstash set('%s') failed: %s; switching to fallback mode.", key, type(exc).__name__)
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return False

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode JSON from cache key '%s': %s", key, exc)
            return None

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> bool:
        try:
            raw = json.dumps(value, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("Failed to serialize JSON for cache key '%s': %s", key, exc)
            return False
        return await self.set(key, raw, ttl=ttl)

    async def delete(self, *keys: str) -> int:
        if not keys or not await self._check_or_reconnect():
            return 0
        try:
            cmd = ["DEL", *keys]
            resp = await self._client.post("/", json=cmd)
            if resp.status_code == 200:
                res = resp.json().get("result")
                return int(res or 0)
            return 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Upstash delete failed: %s; switching to fallback mode.", type(exc).__name__)
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return 0

    async def delete_pattern(self, pattern: str) -> int:
        """Scan and delete all keys matching pattern with safe cursor pagination."""
        if not await self._check_or_reconnect():
            return 0
        total_deleted = 0
        cursor: str | int = "0"
        seen_cursors: set[str] = set()

        try:
            while True:
                cursor_str = str(cursor)
                if cursor_str in seen_cursors and cursor_str != "0":
                    break
                seen_cursors.add(cursor_str)

                cmd = ["SCAN", cursor_str, "MATCH", pattern, "COUNT", 100]
                resp = await self._client.post("/", json=cmd)
                if resp.status_code != 200:
                    break

                data = resp.json()
                res = data.get("result")
                if not isinstance(res, (list, tuple)) or len(res) < 2:
                    break

                next_cursor = res[0]
                keys = res[1] or []

                if keys:
                    # Batch delete in chunks of 100
                    for i in range(0, len(keys), 100):
                        chunk = keys[i : i + 100]
                        total_deleted += await self.delete(*chunk)

                # Upstash returns cursor as string or int ("0" or 0 when complete)
                if str(next_cursor) == "0":
                    break
                cursor = next_cursor

            return total_deleted
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Upstash delete_pattern('%s') failed: %s; switching to fallback mode.", pattern, type(exc).__name__
            )
            self._is_connected = False
            self._last_reconnect_attempt = time.monotonic()
            return total_deleted


class UnifiedRedisClient:
    """Unified client routing to either Local RedisManager or UpstashRedisManager."""

    def __init__(self) -> None:
        self._local_manager = RedisManager()
        self._upstash_manager = UpstashRedisManager()
        self._active: RedisManager | UpstashRedisManager = self._local_manager
        self._provider: str = "local"

    @property
    def is_enabled(self) -> bool:
        return self._active.is_enabled

    @property
    def is_connected(self) -> bool:
        return self._active.is_connected

    @property
    def provider(self) -> str:
        return self._provider

    async def init(
        self,
        redis_url: str = "redis://127.0.0.1:6379/0",
        enabled: bool = True,
        provider: str = "local",
        upstash_url: str | None = None,
        upstash_token: str | None = None,
    ) -> None:
        self._provider = (provider or "local").lower().strip()
        if self._provider == "upstash":
            self._active = self._upstash_manager
            # NEVER initialize local Redis or connect to 127.0.0.1
            await self._upstash_manager.init(
                url=upstash_url,
                token=upstash_token,
                enabled=enabled,
            )
        else:
            self._active = self._local_manager
            await self._local_manager.init(
                redis_url=redis_url,
                enabled=enabled,
            )

    async def close(self) -> None:
        await self._active.close()

    async def ping(self) -> bool:
        return await self._active.ping()

    async def get(self, key: str) -> str | None:
        return await self._active.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        return await self._active.set(key, value, ttl=ttl)

    async def get_json(self, key: str) -> Any | None:
        return await self._active.get_json(key)

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> bool:
        return await self._active.set_json(key, value, ttl=ttl)

    async def delete(self, *keys: str) -> int:
        return await self._active.delete(*keys)

    async def delete_pattern(self, pattern: str) -> int:
        return await self._active.delete_pattern(pattern)


redis_client = UnifiedRedisClient()


# Cache Invalidation Helpers
async def invalidate_member_cache() -> None:
    """Invalidate all member list caches, celebrations, and dashboard counts."""
    await redis_client.delete_pattern("cache:members:*")
    await redis_client.delete_pattern("cache:dashboard:*")
    await redis_client.delete_pattern("cache:celebrations:*")


async def invalidate_group_cache() -> None:
    """Invalidate all group list caches, celebrations, and dashboard counts."""
    await redis_client.delete_pattern("cache:groups:*")
    await redis_client.delete_pattern("cache:dashboard:*")
    await redis_client.delete_pattern("cache:celebrations:*")


async def invalidate_dashboard_cache() -> None:
    """Invalidate dashboard count caches."""
    await redis_client.delete_pattern("cache:dashboard:*")
