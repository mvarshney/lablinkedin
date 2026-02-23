"""
Mock Ranking Service — replaces NVIDIA Triton + Deep FM for learning purposes.

In production:
  • The model (Deep FM, DLRM, or similar CTR model) is compiled to TensorRT.
  • Served by Triton Inference Server via its HTTP/gRPC API.
  • The API service calls POST /v2/models/ranker/infer with feature tensors.

Here we implement the same HTTP interface but score candidates with a
hand-crafted heuristic + small random perturbation to simulate model variance.

When the Feast Feature Store is active (Stage 3 of the feed pipeline), each
candidate's post_features dict includes Feast-sourced signals:
  • affinity_score    — user→author historical engagement (float [0,1])
  • topic_similarity  — cosine(user interest_vector, post embedding) (float [0,1])

Score formula (weights updated to incorporate Feast cross-features):
  pClick = 0.35 * recency_score
         + 0.25 * engagement_score    (likes + has_media)
         + 0.20 * author_affinity     (Feast affinity_score, fallback: avg_engagement)
         + 0.15 * topic_similarity    (Feast cross-feature, fallback: 0.5)
         + 0.05 * jitter              (random noise — simulates model uncertainty)

This gives a realistic distribution: viral recent posts with high topic
relevance and strong author affinity rank highest.
"""
import logging
import math
import random
import time

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel
from prometheus_client import Histogram, make_asgi_app

from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── OTel ──────────────────────────────────────────────────────────────────
resource = Resource.create({"service.name": settings.service_name})
provider = TracerProvider(resource=resource)
try:
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint, insecure=True
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
except Exception:
    pass
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# ── Prometheus ─────────────────────────────────────────────────────────────
RANKING_LATENCY = Histogram(
    "ranking_latency_seconds",
    "Time spent scoring a batch of candidates",
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5],
)

# ── Schemas ────────────────────────────────────────────────────────────────

class Candidate(BaseModel):
    post_id: str
    source: str = "social"
    post_features: dict = {}


class RankRequest(BaseModel):
    user_features: dict = {}
    candidates: list[Candidate]


class PostScore(BaseModel):
    post_id: str
    score: float


class RankResponse(BaseModel):
    scores: list[PostScore]


# ── Scoring logic ──────────────────────────────────────────────────────────

def _score_candidate(user_features: dict, candidate: Candidate) -> float:
    """
    Compute a synthetic pClick score in [0, 1].

    Features used:
      Post (from Feast post_stats):
        like_count, created_at_ts, has_media, content_length
        affinity_score   — user→author affinity (Feast cross-feature)
        topic_similarity — cosine(interest_vector, post_embedding) (Feast ODFV)
      User (from Feast user_stats):
        avg_engagement_rate (or legacy avg_engagement key)
    """
    pf = candidate.post_features
    now = time.time()

    # Recency: exponential decay, half-life = 24 hours
    age_hours = max(0, (now - pf.get("created_at_ts", now)) / 3600)
    recency = math.exp(-age_hours / 24)

    # Engagement: normalise like_count (logistic squeeze)
    likes = pf.get("like_count", 0) or 0
    engagement = likes / (likes + 10)   # asymptotes to 1 as likes → ∞

    # Media bonus
    media_bonus = 0.1 if pf.get("has_media", 0) else 0.0

    # Author affinity (Feast cross-feature).
    # Falls back to avg_engagement from user features when Feast is not active.
    author_affinity = float(pf.get("affinity_score") or 0.0)
    if author_affinity == 0.0:
        user_eng = user_features.get("avg_engagement_rate") \
                   or user_features.get("avg_engagement", 0.5)
        author_affinity = min(float(user_eng), 1.0)

    # Topic similarity (Feast ODFV: cosine of interest_vector vs post embedding).
    # Default 0.5 (neutral) when Feast is not available.
    topic_sim = float(pf.get("topic_similarity") or 0.5)
    topic_sim = max(0.0, min(1.0, (topic_sim + 1.0) / 2.0))  # shift [−1,1] → [0,1]

    # Jitter: ±10% random noise to simulate model uncertainty
    jitter = random.uniform(-0.10, 0.10)

    score = (
        0.35 * recency
        + 0.25 * (engagement + media_bonus)
        + 0.20 * author_affinity
        + 0.15 * topic_sim
        + 0.05 * (0.5 + jitter)      # centre jitter at 0.5
    )
    return round(max(0.0, min(1.0, score)), 4)


# ── App ────────────────────────────────────────────────────────────────────

app = FastAPI(title="Ranking Service (Mock Triton)", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)
app.mount("/metrics", make_asgi_app())


@app.post("/rank", response_model=RankResponse)
def rank(request: RankRequest):
    """
    Score a batch of candidate posts for a given user.

    In production this endpoint would forward requests to a Triton gRPC server
    and return model inference results. Here we compute heuristic scores.
    """
    with tracer.start_as_current_span("rank_candidates") as span:
        t0 = time.perf_counter()

        scores = [
            PostScore(
                post_id=c.post_id,
                score=_score_candidate(request.user_features, c),
            )
            for c in request.candidates
        ]

        latency = time.perf_counter() - t0
        RANKING_LATENCY.observe(latency)

        span.set_attribute("batch.size", len(request.candidates))
        span.set_attribute("ranking.latency_ms", round(latency * 1000, 2))

        return RankResponse(scores=scores)


@app.get("/health")
def health():
    return {"status": "ok", "service": settings.service_name}
