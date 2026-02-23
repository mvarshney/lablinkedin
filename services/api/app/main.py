"""
Social Feed API — entry point.

Startup sequence:
  1. Configure OTel tracing (→ Jaeger via OTLP)
  2. Initialise DB connection pool (TiDB)
  3. Create tables if not present
  4. Start Kafka producer
  5. Connect to Redis
  6. Connect to Qdrant & ensure collection exists
  7. Initialise MinIO client & bucket
  8. Start async HTTP clients (Pinot, Ranking, Feast)
  9. Expose Prometheus /metrics endpoint
"""
import logging

from contextlib import asynccontextmanager
from fastapi import FastAPI
from prometheus_client import make_asgi_app

from app.config import settings
from app.database import init_db
from app.telemetry import setup_tracing, instrument_app
from app.clients.kafka_producer import init_kafka, stop_kafka
from app.clients.redis_client import init_redis
from app.clients.qdrant_client import init_qdrant
from app.clients.minio_client import init_minio
from app.clients.feast_client import feast_client
from app.clients.pinot_client import pinot_client
from app.clients.ranking_client import ranking_client
from app.routers import users, posts, feed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Set up tracing before the app is created so all imports are instrumented
setup_tracing()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of all external connections."""
    logger.info("Starting Social Feed API (env=%s)", settings.environment)

    await init_db()
    await init_kafka()
    await init_redis()
    await init_qdrant()
    init_minio()                    # sync — boto3 is not async
    await pinot_client.start()
    await ranking_client.start()
    await feast_client.start()

    logger.info("All services connected. API ready.")
    yield

    logger.info("Shutting down...")
    await stop_kafka()
    await pinot_client.stop()
    await ranking_client.stop()
    await feast_client.stop()


app = FastAPI(
    title="Social Feed API",
    description=(
        "Hybrid discovery feed system: social graph fan-out + "
        "AI-driven vector similarity ranking."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ── Routers ────────────────────────────────────────────────────────────────
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(posts.router, prefix="/posts", tags=["Posts"])
app.include_router(feed.router, prefix="/feed", tags=["Feed"])

# ── Prometheus metrics endpoint ────────────────────────────────────────────
# Mounted at /metrics — scraped by Prometheus
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

# ── OTel FastAPI instrumentation ──────────────────────────────────────────
instrument_app(app)


@app.get("/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": settings.service_name}
