"""
Async Kafka producer.

Publishes two event types:
  new-posts    — emitted by PostService after a post is persisted.
                 Consumed by: embedding-worker, fanout-worker.
  impressions  — emitted by the feed endpoint when posts are served.
                 Consumed by: a future Pinot Kafka connector.
"""
import json
import logging
from typing import Optional

from aiokafka import AIOKafkaProducer

from app.config import settings

logger = logging.getLogger(__name__)

_producer: Optional[AIOKafkaProducer] = None


async def init_kafka() -> None:
    global _producer
    _producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        acks="all",          # wait for all in-sync replicas
        enable_idempotence=True,
    )
    await _producer.start()
    logger.info(
        "Kafka producer started → %s", settings.kafka_bootstrap_servers
    )


async def stop_kafka() -> None:
    if _producer:
        await _producer.stop()


def get_producer() -> AIOKafkaProducer:
    if _producer is None:
        raise RuntimeError("Kafka producer not initialised")
    return _producer


async def publish_new_post(post_id: str, user_id: str, content: str | None) -> None:
    """
    Emit a NewPost event to the 'new-posts' Kafka topic.

    Schema:
      { post_id, user_id, content }

    Embedding-worker will call an embedding model and store the result in Qdrant.
    Fan-out worker will push post_id to followers' Redis mailboxes.
    """
    producer = get_producer()
    payload = {"post_id": post_id, "user_id": user_id, "content": content or ""}
    await producer.send_and_wait(settings.kafka_topic_new_posts, payload)
    logger.debug("Published NewPost event for post_id=%s", post_id)


async def publish_impressions(user_id: str, post_ids: list[str]) -> None:
    """
    Emit impression events so Pinot can track which posts each user has seen.
    Each post gets its own message so Pinot ingestion is row-level.
    """
    producer = get_producer()
    import time
    ts = int(time.time() * 1000)  # milliseconds

    for post_id in post_ids:
        payload = {"user_id": user_id, "post_id": post_id, "timestamp": ts}
        await producer.send(settings.kafka_topic_impressions, payload)
    logger.debug(
        "Published %d impression events for user_id=%s", len(post_ids), user_id
    )
