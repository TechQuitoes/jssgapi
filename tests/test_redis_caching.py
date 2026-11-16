from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fakeredis import TcpFakeServer
from fastapi.testclient import TestClient

from app.api.v1.translate import TranslateRequest, TranslateResponse, translate
from app.config import get_settings
from app.core.redis import (
    RedisManager,
    UnifiedRedisClient,
    UpstashRedisManager,
    invalidate_dashboard_cache,
    invalidate_group_cache,
    invalidate_member_cache,
    redis_client,
)
from app.main import app
from app.services.translate_service import TranslationResult


@pytest.fixture(scope="module")
def redis_server():
    """Start an in-memory TCP fake redis server if real Redis is not running."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", 6379))
        s.close()
        # Real Redis is already running on 6379
        yield None
        return
    except OSError:
        pass

    server = TcpFakeServer(("127.0.0.1", 6379))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    yield server
    server.shutdown()


@pytest.mark.asyncio
async def test_redis_manager_crud(redis_server):
    """Test RedisManager get, set, get_json, set_json, delete, delete_pattern."""
    mgr = RedisManager()
    await mgr.init("redis://127.0.0.1:6379/0", enabled=True)
    assert mgr.is_connected is True
    assert await mgr.ping() is True

    # Test basic string
    assert await mgr.set("test:key", "value1", ttl=60) is True
    assert await mgr.get("test:key") == "value1"

    # Test JSON
    data = {"name": "JSSG", "count": 42, "items": ["a", "b"]}
    assert await mgr.set_json("test:json", data, ttl=60) is True
    assert await mgr.get_json("test:json") == data

    # Test delete
    assert await mgr.delete("test:key") == 1
    assert await mgr.get("test:key") is None

    # Test delete_pattern
    await mgr.set("cache:test:1", "a")
    await mgr.set("cache:test:2", "b")
    await mgr.set("cache:other:3", "c")
    deleted = await mgr.delete_pattern("cache:test:*")
    assert deleted == 2
    assert await mgr.get("cache:test:1") is None
    assert await mgr.get("cache:other:3") == "c"

    await mgr.close()


@pytest.mark.asyncio
async def test_redis_graceful_fallback_when_disabled():
    """When REDIS_ENABLED=False, manager operations gracefully return None/False without errors."""
    mgr = RedisManager()
    await mgr.init("redis://127.0.0.1:6379/0", enabled=False)
    assert mgr.is_enabled is False
    assert mgr.is_connected is False
    assert await mgr.ping() is False

    assert await mgr.get("any_key") is None
    assert await mgr.set("any_key", "val") is False
    assert await mgr.get_json("any_key") is None
    assert await mgr.set_json("any_key", {"a": 1}) is False
    assert await mgr.delete("any_key") == 0
    assert await mgr.delete_pattern("any:*") == 0
    await mgr.close()


@pytest.mark.asyncio
async def test_redis_graceful_fallback_when_unreachable():
    """When Redis server is unreachable, operations do not raise exceptions."""
    mgr = RedisManager()
    # Port 6399 is not running
    await mgr.init("redis://127.0.0.1:6399/0", enabled=True)
    assert mgr.is_connected is False
    assert await mgr.ping() is False
    assert await mgr.get("key") is None
    assert await mgr.set("key", "val") is False
    await mgr.close()


@pytest.mark.asyncio
async def test_translate_caching_hit_and_miss(redis_server):
    """Test Translation API caching: cache miss calls service, cache hit returns cached."""
    await redis_client.init("redis://127.0.0.1:6379/0", enabled=True)
    await redis_client.delete_pattern("cache:translate:*")

    mock_result = TranslationResult(
        original_text="Hello world",
        translated_text="नमस्ते दुनिया",
        source_language="en",
        target_language="hi",
        detected_language="en",
    )

    req = TranslateRequest(
        text="Hello world", target_language="hi", source_language="en"
    )

    with patch(
        "app.api.v1.translate.translate_text", return_value=mock_result
    ) as mock_translate:
        # First call: Cache MISS -> invokes translate_text
        res1 = await translate(req)
        assert res1.translatedText == "नमस्ते दुनिया"
        assert mock_translate.call_count == 1

        # Second call: Cache HIT -> returns cached, translate_text NOT called again
        res2 = await translate(req)
        assert res2.translatedText == "नमस्ते दुनिया"
        assert mock_translate.call_count == 1  # Unchanged!

    # Verify cache key structure
    text_hash = hashlib.sha256(b"Hello world").hexdigest()
    expected_key = f"cache:translate:en:hi:{text_hash}"
    cached_val = await redis_client.get_json(expected_key)
    assert cached_val is not None
    assert cached_val["translatedText"] == "नमस्ते दुनिया"

    await redis_client.close()


@pytest.mark.asyncio
async def test_dashboard_statistics_caching(redis_server):
    """Test Dashboard Statistics caching: first call queries DB, second call uses cache."""
    await redis_client.init("redis://127.0.0.1:6379/0", enabled=True)
    await invalidate_dashboard_cache()

    cache_key = "cache:dashboard:statistics"
    assert await redis_client.get_json(cache_key) is None

    # Simulate seeded cached data
    cached_payload = {
        "total_members": 150,
        "total_groups": 12,
        "total_post_holders": 24,
        "total_cities": 5,
    }
    await redis_client.set_json(cache_key, cached_payload, ttl=600)

    # Calling dashboard endpoint should fetch from cache directly
    cached_result = await redis_client.get_json(cache_key)
    assert cached_result == cached_payload

    # Test cache invalidation
    await invalidate_dashboard_cache()
    assert await redis_client.get_json(cache_key) is None

    await redis_client.close()


@pytest.mark.asyncio
async def test_member_and_group_cache_invalidation(redis_server):
    """Test member and group cache invalidation triggers."""
    await redis_client.init("redis://127.0.0.1:6379/0", enabled=True)

    # Populate dummy cache keys
    await redis_client.set("cache:members:unrestricted:True:scope_m:none:page:1", "m1")
    await redis_client.set(
        "cache:members:unrestricted:False:scope_m:none:page:1", "m2"
    )
    await redis_client.set("cache:groups:src:db:field:all:page:1", "g1")
    await redis_client.set("cache:dashboard:statistics", "d1")

    # Invalidate member cache -> clears members AND dashboard
    await invalidate_member_cache()
    assert (
        await redis_client.get(
            "cache:members:unrestricted:True:scope_m:none:page:1"
        )
        is None
    )
    assert (
        await redis_client.get(
            "cache:members:unrestricted:False:scope_m:none:page:1"
        )
        is None
    )
    assert await redis_client.get("cache:dashboard:statistics") is None
    # Groups should still be intact
    assert await redis_client.get("cache:groups:src:db:field:all:page:1") == "g1"

    # Reset dashboard, test group invalidation -> clears groups AND dashboard
    await redis_client.set("cache:dashboard:statistics", "d2")
    await invalidate_group_cache()
    assert await redis_client.get("cache:groups:src:db:field:all:page:1") is None
    assert await redis_client.get("cache:dashboard:statistics") is None

    await redis_client.close()


@pytest.mark.asyncio
async def test_group_list_member_count_aggregation(redis_server):
    """Test group listing serialization and caching with member_count."""
    import uuid
    from app.api.v1.groups import _serialize_groups_with_counts

    await redis_client.init("redis://127.0.0.1:6379/0", enabled=True)
    await invalidate_group_cache()

    g_id = uuid.uuid4()
    mock_group = MagicMock()
    mock_group.id = g_id
    mock_group.group_id = "MP09001"
    mock_group.name = "Test Group"
    mock_group.state = "Madhya Pradesh"
    mock_group.city = "Indore"
    mock_group.description = None
    mock_group.contact_name = None
    mock_group.contact_email = None
    mock_group.contact_phone = None
    mock_group.address = None
    mock_group.logo_url = None
    mock_group.status = "ACTIVE"
    mock_group.is_active = True
    mock_group.location_id = None
    mock_group.admins = []
    mock_group.created_at = "2026-01-01T00:00:00"
    mock_group.updated_at = "2026-01-01T00:00:00"

    mock_repo = MagicMock()
    mock_repo.get_member_counts = AsyncMock(return_value={g_id: 42})

    serialized = await _serialize_groups_with_counts(mock_repo, [mock_group])
    assert len(serialized) == 1
    assert serialized[0]["member_count"] == 42
    assert serialized[0]["name"] == "Test Group"

    # Test caching serialized group response in Redis
    cache_key = "cache:groups:src:db:field:all:status:all:q:all:page:1:size:20"
    payload = {"data": serialized, "meta": {"page": 1, "page_size": 20, "total": 1}}
    await redis_client.set_json(cache_key, payload, ttl=900)

    cached_res = await redis_client.get_json(cache_key)
    assert cached_res["data"][0]["member_count"] == 42

    await invalidate_group_cache()
    assert await redis_client.get_json(cache_key) is None

    await redis_client.close()


@pytest.mark.asyncio
async def test_upstash_redis_manager_crud():
    """Test UpstashRedisManager GET, SET, DEL, PING using a mocked httpx transport."""
    store: dict[str, str] = {}

    def upstash_handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        data = _json.loads(request.content.decode("utf-8"))
        cmd = data[0].upper()
        if cmd == "PING":
            return httpx.Response(200, json={"result": "PONG"})
        elif cmd == "GET":
            key = data[1]
            return httpx.Response(200, json={"result": store.get(key)})
        elif cmd == "SET":
            key, val = data[1], data[2]
            store[key] = str(val)
            return httpx.Response(200, json={"result": "OK"})
        elif cmd == "DEL":
            keys = data[1:]
            cnt = sum(1 for k in keys if store.pop(k, None) is not None)
            return httpx.Response(200, json={"result": cnt})
        elif cmd == "SCAN":
            all_keys = list(store.keys())
            return httpx.Response(200, json={"result": ["0", all_keys]})
        return httpx.Response(400, json={"error": f"Unknown command {cmd}"})

    mgr = UpstashRedisManager(timeout=2.0)
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstash_handler),
        base_url="https://mock.upstash.io",
    )
    mgr._enabled = True
    mgr._is_connected = True

    # Ping
    assert await mgr.ping() is True

    # Set / Get string
    assert await mgr.set("k1", "val1", ttl=60) is True
    assert await mgr.get("k1") == "val1"

    # Set / Get JSON
    assert await mgr.set_json("k2", {"foo": "bar", "num": 123}, ttl=60) is True
    assert await mgr.get_json("k2") == {"foo": "bar", "num": 123}

    # Delete
    assert await mgr.delete("k1") == 1
    assert await mgr.get("k1") is None

    # Delete pattern
    await mgr.set("cache:test:1", "a")
    await mgr.set("cache:test:2", "b")
    deleted = await mgr.delete_pattern("cache:test:*")
    assert deleted >= 2
    assert await mgr.get("cache:test:1") is None

    await mgr.close()


@pytest.mark.asyncio
async def test_upstash_scan_cursor_pagination():
    """Test Upstash SCAN cursor pagination handles string/int cursors, multi-batch deletions, and empty batches."""
    calls: list[str] = []

    def multi_batch_handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        data = _json.loads(request.content.decode("utf-8"))
        cmd = data[0].upper()
        if cmd == "SCAN":
            cur = str(data[1])
            calls.append(cur)
            if cur == "0":
                # Batch 1 returns keys and next cursor "cursor_1"
                return httpx.Response(200, json={"result": ["cursor_1", ["k1", "k2"]]})
            elif cur == "cursor_1":
                # Batch 2 returns next cursor as an integer 2 and empty keys
                return httpx.Response(200, json={"result": [2, []]})
            elif cur == "2":
                # Batch 3 returns keys and completion cursor "0"
                return httpx.Response(200, json={"result": ["0", ["k3"]]})
        elif cmd == "DEL":
            keys = data[1:]
            return httpx.Response(200, json={"result": len(keys)})
        return httpx.Response(200, json={"result": None})

    mgr = UpstashRedisManager()
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(multi_batch_handler),
        base_url="https://mock.upstash.io",
    )
    mgr._enabled = True
    mgr._is_connected = True

    deleted = await mgr.delete_pattern("pattern:*")
    assert deleted == 3
    assert calls == ["0", "cursor_1", "2"]

    await mgr.close()


@pytest.mark.asyncio
async def test_upstash_error_fallback_and_cancelled_error():
    """Verify Upstash catches network errors safely and does not swallow asyncio.CancelledError."""

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Connection timed out")

    mgr = UpstashRedisManager()
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(failing_handler),
        base_url="https://failing.upstash.io",
    )
    mgr._enabled = True
    mgr._is_connected = True

    # Failures return fallback values (None, False, 0) without raising
    assert await mgr.get("some_key") is None
    assert await mgr.set("some_key", "val") is False
    assert await mgr.delete("some_key") == 0
    assert await mgr.delete_pattern("some:*") == 0

    # CancelledError must NOT be swallowed
    def cancelling_handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    mgr._is_connected = True
    mgr._client = httpx.AsyncClient(
        transport=httpx.MockTransport(cancelling_handler),
        base_url="https://cancelling.upstash.io",
    )
    with pytest.raises(asyncio.CancelledError):
        await mgr.get("cancelled_key")

    await mgr.close()


@pytest.mark.asyncio
async def test_unified_redis_client_provider_isolation():
    """Verify provider=upstash never initializes or connects to local Redis."""
    client = UnifiedRedisClient()

    # Mock the underlying managers to inspect initialization calls
    client._local_manager.init = AsyncMock()
    client._upstash_manager.init = AsyncMock()

    # 1. Initialize as local
    await client.init(
        redis_url="redis://127.0.0.1:6379/0",
        enabled=True,
        provider="local",
    )
    assert client.provider == "local"
    client._local_manager.init.assert_called_once()
    client._upstash_manager.init.assert_not_called()

    client._local_manager.init.reset_mock()
    client._upstash_manager.init.reset_mock()

    # 2. Initialize as upstash
    await client.init(
        redis_url="redis://127.0.0.1:6379/0",
        enabled=True,
        provider="upstash",
        upstash_url="https://my-upstash.upstash.io",
        upstash_token="my-token",
    )
    assert client.provider == "upstash"
    # Local Redis MUST NOT be initialized
    client._local_manager.init.assert_not_called()
    client._upstash_manager.init.assert_called_once_with(
        url="https://my-upstash.upstash.io",
        token="my-token",
        enabled=True,
    )
