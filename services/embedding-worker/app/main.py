"""
Embedding Worker — Kafka consumer.

For every 'new-posts' event:
  1. Extract the post content.
  2. Generate a 384-dim embedding (sentence-transformers).
  3. Upsert the vector into Qdrant with metadata payload.

The Qdrant collection is later queried by the API's Discovery retrieval stage
to surface out-of-network content via ANN (approximate nearest neighbour) search.

Concurrency model: single-threaded event loop; CPU-bound embedding is run
in an executor to avoid blocking the asyncio loop.
"""
import asyncio
import json
import logging
import time

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

from aiokafka import AIOKafkaConsumer
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.config import settings
from app.embedder import embed_text, load_model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


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


tracer = None


# ─────────────────────────── Qdrant Helper ───────────────────────────────

async def ensure_collection(client: AsyncQdrantClient) -> None:
    existing = await client.get_collections()
    names = [c.name for c in existing.collections]
    if settings.qdrant_collection not in names:
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(
                size=settings.embedding_dimension,
                distance=Distance.COSINE,
            ),
        )
        logger.info("Created Qdrant collection '%s'", settings.qdrant_collection)


# ─────────────────────────── Main Loop ───────────────────────────────────

async def process_message(msg: dict, qdrant: AsyncQdrantClient) -> None:
    """
    Handle a single NewPost event.

    payload = { "post_id": str, "user_id": str, "content": str }
    """
    post_id = msg.get("post_id")
    user_id = msg.get("user_id")
    content = msg.get("content", "")

    if not post_id or not user_id:
        logger.warning("Malformed message — missing post_id or user_id: %s", msg)
        return

    with tracer.start_as_current_span("embed_and_store") as span:
        span.set_attribute("post.id", post_id)
        span.set_attribute("post.user_id", user_id)

        t0 = time.perf_counter()

        # Run CPU-bound embedding in thread pool to avoid blocking the loop
        loop = asyncio.get_event_loop()
        vector = await loop.run_in_executor(
            None, embed_text, content or post_id, settings.embedding_dimension
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000
        span.set_attribute("embedding.latency_ms", round(elapsed_ms, 2))

        # Store in Qdrant
        await qdrant.upsert(
            collection_name=settings.qdrant_collection,
            points=[
                PointStruct(
                    id=post_id,
                    vector=vector,
                    payload={
                        "user_id": user_id,
                        "created_at_ts": time.time(),
                        "content_preview": content[:200] if content else "",
                    },
                )
            ],
        )
        logger.info(
            "Embedded post %s (%.1f ms) → Qdrant", post_id, elapsed_ms
        )


async def main() -> None:
    setup_tracing()
    global tracer
    tracer = trace.get_tracer(__name__)

    # Load the embedding model (may download on first run)
    load_model(settings.embedding_model)

    qdrant = AsyncQdrantClient(
        host=settings.qdrant_host, port=settings.qdrant_port
    )
    await ensure_collection(qdrant)

    consumer = AIOKafkaConsumer(
        settings.kafka_topic_new_posts,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_consumer_group,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    )
    await consumer.start()
    logger.info(
        "Embedding worker listening on topic '%s'", settings.kafka_topic_new_posts
    )

    try:
        async for msg in consumer:
            try:
                await process_message(msg.value, qdrant)
            except Exception as exc:
                logger.error("Failed to process message: %s — %s", msg.value, exc)
    finally:
        await consumer.stop()
        await qdrant.close()


if __name__ == "__main__":
    asyncio.run(main())
