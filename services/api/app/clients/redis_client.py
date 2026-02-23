"""
Redis client wrapper.

Responsibilities:
  • Feed mailboxes  — ZSET keyed by feed:{user_id}
                       score = post creation timestamp (Unix)
                       member = post_id
  • User features   — HASH keyed by uf:{user_id}
  • Post features   — HASH keyed by pf:{post_id}
  • Interest vector — STRING (JSON) keyed by iv:{user_id}

Fan-out worker writes to feed mailboxes.
API reads from them during Stage-1 retrieval.
"""
import json
import logging
import time
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_redis: Optional[aioredis.Redis] = None


async def init_redis() -> None:
    global _redis
    _redis = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )
    await _redis.ping()
    logger.info("Redis connected at %s:%s", settings.redis_host, settings.redis_port)


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised — call init_redis() at startup")
    return _redis


# ─────────────────────── Feed Mailbox (ZSET) ──────────────────────────────

FEED_KEY = "feed:{user_id}"


async def get_social_feed(user_id: str, limit: int = 100) -> list[str]:
    """
    Fetch the top `limit` post_ids from the user's social feed mailbox.
    ZREVRANGEBYSCORE returns highest-score (newest) items first.
    """
    r = get_redis()
    key = f"feed:{user_id}"
    post_ids: list[str] = await r.zrevrange(key, 0, limit - 1)
    return post_ids


async def push_to_feed(user_id: str, post_id: str, score: float | None = None) -> None:
    """
    Push a post_id into the user's ZSET mailbox.
    score defaults to the current Unix timestamp so newest posts rank highest.
    Trims the mailbox to redis_feed_max_size to bound memory.
    """
    r = get_redis()
    key = f"feed:{user_id}"
    if score is None:
        score = time.time()
    await r.zadd(key, {post_id: score})
    # Keep mailbox bounded
    await r.zremrangebyrank(key, 0, -(settings.redis_feed_max_size + 1))
    await r.expire(key, settings.redis_feed_ttl)


# ─────────────────────── User / Post Feature Cache ────────────────────────

async def get_user_features(user_id: str) -> dict:
    r = get_redis()
    raw = await r.hgetall(f"uf:{user_id}")
    return {k: float(v) for k, v in raw.items()}


async def set_user_features(user_id: str, features: dict) -> None:
    r = get_redis()
    await r.hset(f"uf:{user_id}", mapping={k: str(v) for k, v in features.items()})
    await r.expire(f"uf:{user_id}", 3600)  # 1h


async def get_post_features(post_ids: list[str]) -> dict[str, dict]:
    """Batch-fetch post feature hashes from Redis."""
    r = get_redis()
    pipe = r.pipeline()
    for pid in post_ids:
        pipe.hgetall(f"pf:{pid}")
    results = await pipe.execute()
    return {
        pid: {k: float(v) for k, v in (res or {}).items()}
        for pid, res in zip(post_ids, results)
    }


async def set_post_features(post_id: str, features: dict) -> None:
    r = get_redis()
    await r.hset(f"pf:{post_id}", mapping={k: str(v) for k, v in features.items()})
    await r.expire(f"pf:{post_id}", 7200)  # 2h


# ─────────────────────── Interest Vector ──────────────────────────────────

async def get_interest_vector(user_id: str) -> list[float] | None:
    r = get_redis()
    raw = await r.get(f"iv:{user_id}")
    if raw:
        return json.loads(raw)
    return None


async def set_interest_vector(user_id: str, vector: list[float]) -> None:
    r = get_redis()
    await r.set(f"iv:{user_id}", json.dumps(vector), ex=86400)
