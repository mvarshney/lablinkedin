"""
Observability setup:
  - OpenTelemetry distributed tracing → Jaeger (via OTLP gRPC)
  - Prometheus metrics: feed_latency_seconds, retrieval_recall_ratio

Both are initialised once at startup and injected into FastAPI via middleware.
"""
import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from prometheus_client import Histogram, Gauge, Counter

from app.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────── Prometheus Metrics ───────────────────────────
FEED_LATENCY = Histogram(
    "feed_latency_seconds",
    "End-to-end latency of GET /feed",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)

RETRIEVAL_RECALL = Gauge(
    "retrieval_recall_ratio",
    "Fraction of candidates surviving impression filtering (higher = more fresh content)",
)

POST_INGESTION_TOTAL = Counter(
    "post_ingestion_total",
    "Total number of posts ingested",
)

FEED_CANDIDATES_TOTAL = Counter(
    "feed_candidates_total",
    "Total candidate posts generated per feed request",
    ["source"],  # 'social' or 'discovery'
)

RANKING_ERRORS_TOTAL = Counter(
    "ranking_errors_total",
    "Number of times the ranking service returned an error (fell back to mock)",
)


# ─────────────────────────── OpenTelemetry Setup ──────────────────────────
def setup_tracing() -> None:
    """Configure the global OTel TracerProvider with OTLP/Jaeger export."""
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "deployment.environment": settings.environment,
        }
    )

    provider = TracerProvider(resource=resource)

    try:
        otlp_exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            insecure=True,
        )
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info(
            "OTel tracing configured → %s", settings.otel_exporter_otlp_endpoint
        )
    except Exception as exc:
        logger.warning("Could not connect to OTLP exporter: %s — traces disabled", exc)

    trace.set_tracer_provider(provider)

    # Auto-instrument popular libraries so their spans appear in traces
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument()


def instrument_app(app) -> None:  # noqa: ANN001
    """Call after app is created to add FastAPI request spans."""
    FastAPIInstrumentor.instrument_app(app)
