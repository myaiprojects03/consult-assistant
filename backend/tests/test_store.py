"""Shared store: rate-limit counting and single-holder locks (Redis when TEST_REDIS_URL is set)."""
import asyncio
import os

import pytest
from redis.asyncio import Redis

from app.shared.store import MemoryStore, RedisStore

TEST_REDIS = os.environ.get("TEST_REDIS_URL")
STORES = ["memory"] + (["redis"] if TEST_REDIS else [])


def make(kind):
    if kind == "memory":
        return MemoryStore()
    return RedisStore(Redis.from_url(TEST_REDIS, decode_responses=True))


@pytest.mark.parametrize("kind", STORES)
def test_lock_has_one_holder_and_can_be_released(kind):
    async def go():
        s = make(kind)
        await s.clear("lock:test")
        assert await s.add("lock:test", "1", 30) is True
        assert await s.add("lock:test", "1", 30) is False
        await s.clear("lock:test")
        assert await s.add("lock:test", "1", 30) is True
        await s.clear("lock:test")
        await s.aclose()
    asyncio.run(go())


@pytest.mark.parametrize("kind", STORES)
def test_hits_count_within_a_window(kind):
    async def go():
        s = make(kind)
        await s.clear("hits:test")
        assert [await s.hit("hits:test", 60) for _ in range(3)] == [1, 2, 3]
        await s.clear("hits:test")
        await s.aclose()
    asyncio.run(go())
