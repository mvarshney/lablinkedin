"""
Fan-out Worker — Kafka consumer.

For every 'new-posts' event:
  1. Query TiDB for all followers of the post author.
  2. For each follower, ZADD the post_id (score = creation timestamp) to
     their Redis feed mailbox (ZSET).
  3. Trim the mailbox to prevent unbounded growth.

Key design decisions:
  • Fan-out on WRITE — pre-populate follower mailboxes immediately.
    This makes reads (GET /feed) very fast (O(1) Redis ZREVRANGE).
  • Celebrity / high-follower bypass — if an author has > fan_out_follower_cap
    followers we skip fan-out and instead rely on pull-on-read (Discovery).
    This prevents hot-writes to Redis for viral accounts.
"""
import asyncio
import json
import logging
import time

import aiomysql
import redis.asyncio as aioredis
from aiokafka import AIOKafkaConsumer
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

tracer = None


# ─────────────────────────── OTel Setup ──────────────────────────────────

def setup_tracing() -> None:
    resource = Resource.create({"service.name": settings.service_name})
    provider = TracerProvider(resource=resource)
    try:
        exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint, insecure=True
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    except Exception as exc:
        logger.warning("OTel exporter unavailable: %s", exc)
    trace.set_tracer_provider(provider)


# ─────────────────────────── DB Helpers ──────────────────────────────────

async def get_followers(
    pool: aiomysql.Pool,
    user_id: str,
    cap: int,
) -> list[str]:
    """
    Return follower IDs for `user_id` up to `cap`.
    Query uses the idx_followee index for efficiency.
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT follower_id FROM follows WHERE followee_id = %s LIMIT %s",
                (user_id, cap),
            )
            rows = await cur.fetchall()
    return [row[0] for row in rows]


# ─────────────────────────── Redis Helpers ───────────────────────────────

async def push_to_feed(
    redis: aioredis.Redis,
    user_id: str,
    post_id: str,
    score: float,
) -> None:
    key = f"feed:{user_id}"
    pipe = redis.pipeline()
    pipe.zadd(key, {post_id: score})
    pipe.zremrangebyrank(key, 0, -(settings.redis_feed_max_size + 1))
    pipe.expire(key, settings.redis_feed_ttl)
    await pipe.execute()


# ─────────────────────────── Message Handler ─────────────────────────────

async def process_message(
    msg: dict,
    db_pool: aiomysql.Pool,
    redis: aioredis.Redis,
) -> None:
    post_id = msg.get("post_id")
    user_id = msg.get("user_id")

    if not post_id or not user_id:
        logger.warning("Malformed NewPost event: %s", msg)
        return

    with tracer.start_as_current_span("fanout") as span:
        span.set_attribute("post.id", post_id)
        span.set_attribute("post.user_id", user_id)

        t0 = time.perf_counter()
        followers = await get_followers(db_pool, user_id, settings.fan_out_follower_cap)
        elapsed_db = (time.perf_counter() - t0) * 1000

        follower_count = len(followers)
        span.set_attribute("fanout.follower_count", follower_count)

        if follower_count == 0:
            logger.info("Post %s — author has no followers, skipping fan-out", post_id)
            return

        if follower_count >= settings.fan_out_follower_cap:
            logger.info(
                "Post %s — author %s is a celebrity (%d+ followers), "
                "skipping fan-out (Discovery will serve this content)",
                post_id, user_id, follower_count,
            )
            return

        # Use current time as score so newest posts rank first in feed
        score = time.time()

        # Push to all follower mailboxes concurrently
        await asyncio.gather(
            *[push_to_feed(redis, fid, post_id, score) for fid in followers]
        )

        elapsed_total = (time.perf_counter() - t0) * 1000
        logger.info(
            "Fan-out complete: post %s → %d followers "
            "(db=%.1fms, total=%.1fms)",
            post_id, follower_count, elapsed_db, elapsed_total,
        )


# ─────────────────────────── Main Loop ───────────────────────────────────

async def main() -> None:
    setup_tracing()
    global tracer
    tracer = trace.get_tracer(__name__)

    # TiDB connection pool
    db_pool = await aiomysql.create_pool(
        host=settings.tidb_host,
        port=settings.tidb_port,
        user=settings.tidb_user,
        password=settings.tidb_password,
        db=settings.tidb_database,
        minsize=2,
        maxsize=10,
        autocommit=True,
    )

    redis = aioredis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )
    await redis.ping()

    consumer = AIOKafkaConsumer(
        settings.kafka_topic_new_posts,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_consumer_group,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    )
    await consumer.start()
    logger.info(
        "Fan-out worker listening on topic '%s'", settings.kafka_topic_new_posts
    )

    try:
        async for msg in consumer:
            try:
                await process_message(msg.value, db_pool, redis)
            except Exception as exc:
                logger.error("Fan-out error for %s: %s", msg.value, exc)
    finally:
        await consumer.stop()
        db_pool.close()
        await db_pool.wait_closed()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
