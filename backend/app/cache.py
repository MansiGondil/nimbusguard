"""Fast state + pub/sub: Redis when configured, otherwise an in-process fallback.

Holds latest device status/metrics, rate-limit counters for the automation
safety gates, and the event bus used to stream updates to WebSocket clients.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)
CHANNEL = "nimbusguard:events"
COMMANDS = "nimbusguard:commands"


class MemoryCache:
    def __init__(self) -> None:
        self._kv: dict[str, tuple[Any, float | None]] = {}
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._kv[key] = (value, time.time() + ttl if ttl else None)

    async def get_json(self, key: str) -> Any:
        item = self._kv.get(key)
        if not item:
            return None
        value, expires = item
        if expires and expires < time.time():
            self._kv.pop(key, None)
            return None
        return value

    async def hit(self, key: str, window_seconds: int) -> int:
        """Record a hit and return the number of hits within the sliding window."""
        now = time.time()
        hits = [t for t in self._hits[key] if t > now - window_seconds]
        hits.append(now)
        self._hits[key] = hits
        return len(hits)

    async def count(self, key: str, window_seconds: int) -> int:
        now = time.time()
        return len([t for t in self._hits[key] if t > now - window_seconds])

    async def publish(self, event: dict, channel: str = CHANNEL) -> None:
        for q in list(self._subscribers[channel]):
            if not q.full():
                q.put_nowait(event)

    async def subscribe(self, channel: str = CHANNEL) -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers[channel].add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers[channel].discard(q)

    async def close(self) -> None:
        pass


class RedisCache(MemoryCache):
    def __init__(self, url: str) -> None:
        super().__init__()
        import redis.asyncio as redis

        self.r = redis.from_url(url, decode_responses=True)

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self.r.set(key, json.dumps(value, default=str), ex=ttl)

    async def get_json(self, key: str) -> Any:
        raw = await self.r.get(key)
        return json.loads(raw) if raw else None

    async def hit(self, key: str, window_seconds: int) -> int:
        now = time.time()
        pipe = self.r.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zadd(key, {f"{now}": now})
        pipe.zcard(key)
        pipe.expire(key, window_seconds)
        _, _, n, _ = await pipe.execute()
        return int(n)

    async def count(self, key: str, window_seconds: int) -> int:
        await self.r.zremrangebyscore(key, 0, time.time() - window_seconds)
        return int(await self.r.zcard(key))

    async def publish(self, event: dict, channel: str = CHANNEL) -> None:
        await self.r.publish(channel, json.dumps(event, default=str))

    async def subscribe(self, channel: str = CHANNEL) -> AsyncIterator[dict]:
        pubsub = self.r.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") == "message":
                    yield json.loads(msg["data"])
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def close(self) -> None:
        await self.r.aclose()


_cache: MemoryCache | None = None


def get_cache() -> MemoryCache:
    global _cache
    if _cache is None:
        url = get_settings().redis_url
        _cache = RedisCache(url) if url else MemoryCache()
        log.info("Cache backend: %s", type(_cache).__name__)
    return _cache


def reset_cache() -> None:
    """Testing helper."""
    global _cache
    _cache = None
