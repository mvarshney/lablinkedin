"""
Feed retrieval endpoint — GET /feed?user_id=<id>

Implements the 4-stage pipeline described in the architecture:

  Stage 1 │ Candidate Generation
  ────────┼──────────────────────────────────────────────────────────────
          │  Social     — Redis ZSET mailbox  (fan-out on write)
          │  Discovery  — Qdrant ANN search   (out-of-network content)

  Stage 2 │ Impression Discounting
  ────────┼──────────────────────────────────────────────────────────────
          │  Query Pinot for post_ids seen in the last 24h.
          │  Remove seen posts from the candidate pool.

  Stage 3 │ ML Ranking (features via Feast Feature Store)
  ────────┼──────────────────────────────────────────────────────────────
          │  Fetch features from Feast Feature Server (primary):
          │    • user_stats   — follower_count, avg_engagement_rate,
          │                     interest_vector_json, …
          │    • post_stats   — like_count, has_media, author_id,
          │                     embedding_json, …
          │    • cross-features computed locally:
          │        topic_similarity  = cosine(interest_vector, embedding)
          │        affinity_score    = user→author historical engagement
          │  Falls back to Redis feature cache if Feast is unavailable.
          │  Send ≤150 candidates to the ranking service → pClick scores.

  Stage 4 │ Re-ranking, Business Rules & Hydration
  ────────┼──────────────────────────────────────────────────────────────
          │  Apply author diversity cap (max 2 posts per author).
          │  Hydrate top-20 post_ids with full metadata from TiDB.
          │  Attach pre-signed MinIO URLs for media.
          │  Emit impression events to Kafka → Pinot.
"""
import logging
import random
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Post, User
from app.schemas import FeedPost, FeedResponse
from app.clients.feast_client import feast_client
from app.clients.kafka_producer import publish_impressions
from app.clients.minio_client import get_presigned_url
from app.clients.pinot_client import pinot_client
from app.clients.qdrant_client import search_similar_posts
from app.clients.ranking_client import ranking_client
from app.clients.redis_client import (
    get_interest_vector,
    get_post_features,
    get_social_feed,
    get_user_features,
    set_interest_vector,
)
from app.config import settings
from app.telemetry import (
    FEED_CANDIDATES_TOTAL,
    FEED_LATENCY,
    RETRIEVAL_RECALL,
)

logger = logging.getLogger(__name__)
router = APIRouter()
tracer = trace.get_tracer(__name__)

MAX_AUTHOR_POSTS = 2       # business rule: diversity cap
MAX_CANDIDATES = 150       # max posts sent to ranking service


@router.get("/", response_model=FeedResponse)
async def get_feed(
    user_id: str = Query(..., description="ID of the requesting user"),
    db: AsyncSession = Depends(get_db),
):
    start_time = time.time()

    with tracer.start_as_current_span("get_feed") as span:
        span.set_attribute("user.id", user_id)

        # ── Validate user ────────────────────────────────────────────────
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # ═══════════════════════════════════════════════════════════════
        #  STAGE 1 — Candidate Generation (run social + discovery in parallel)
        # ═══════════════════════════════════════════════════════════════
        with tracer.start_as_current_span("stage1_retrieval"):

            # Ensure user has an interest vector (fallback: random)
            interest_vector = await get_interest_vector(user_id)
            if interest_vector is None:
                interest_vector = [random.uniform(-1, 1) for _ in range(settings.embedding_dimension)]
                await set_interest_vector(user_id, interest_vector)

            import asyncio
            social_ids, discovery_ids = await asyncio.gather(
                get_social_feed(user_id, limit=settings.ranking_candidate_limit),
                search_similar_posts(
                    interest_vector,
                    limit=settings.ranking_candidate_limit,
                    exclude_user_id=user_id,
                ),
            )

        FEED_CANDIDATES_TOTAL.labels(source="social").inc(len(social_ids))
        FEED_CANDIDATES_TOTAL.labels(source="discovery").inc(len(discovery_ids))
        span.set_attribute("candidates.social", len(social_ids))
        span.set_attribute("candidates.discovery", len(discovery_ids))

        # Merge with source tag; dedup keeping first occurrence
        seen_in_merge: set[str] = set()
        candidates: list[dict] = []
        for pid in social_ids:
            if pid not in seen_in_merge:
                candidates.append({"post_id": pid, "source": "social"})
                seen_in_merge.add(pid)
        for pid in discovery_ids:
            if pid not in seen_in_merge:
                candidates.append({"post_id": pid, "source": "discovery"})
                seen_in_merge.add(pid)

        # ═══════════════════════════════════════════════════════════════
        #  STAGE 2 — Impression Discounting
        # ═══════════════════════════════════════════════════════════════
        with tracer.start_as_current_span("stage2_impression_filtering"):
            seen_post_ids = await pinot_client.get_seen_post_ids(user_id)

        before_filter = len(candidates)
        candidates = [c for c in candidates if c["post_id"] not in seen_post_ids]
        after_filter = len(candidates)

        recall_ratio = after_filter / before_filter if before_filter else 1.0
        RETRIEVAL_RECALL.set(recall_ratio)
        span.set_attribute("candidates.after_filter", after_filter)
        span.set_attribute("impression.recall_ratio", recall_ratio)

        # Cap before sending to ranking
        candidates = candidates[:MAX_CANDIDATES]

        # ═══════════════════════════════════════════════════════════════
        #  STAGE 3 — ML Ranking  (Feast features → ranking service)
        # ═══════════════════════════════════════════════════════════════
        with tracer.start_as_current_span("stage3_ranking"):
            candidate_ids = [c["post_id"] for c in candidates]

            # ── Feature retrieval: Feast first, Redis fallback ─────────
            with tracer.start_as_current_span("stage3_feature_fetch"):
                try:
                    user_features, post_feature_map = (
                        await feast_client.get_ranking_features(user_id, candidate_ids)
                    )
                    # Enrich each post with user→author affinity score
                    author_ids = [
                        post_feature_map.get(pid, {}).get("author_id")
                        for pid in candidate_ids
                    ]
                    unique_authors = [a for a in set(author_ids) if a]
                    affinity_map = await feast_client.get_affinity_features(
                        user_id, unique_authors
                    )
                    for pid in candidate_ids:
                        pf = post_feature_map.get(pid, {})
                        aid = pf.get("author_id")
                        pf["affinity_score"] = affinity_map.get(aid, 0.0) if aid else 0.0

                    span.set_attribute("stage3.feature_source", "feast")

                except Exception as exc:
                    # Feast unavailable — degrade gracefully to Redis cache
                    logger.warning(
                        "Feast feature fetch failed (%s) — falling back to Redis", exc
                    )
                    user_features, post_feature_map = await asyncio.gather(
                        get_user_features(user_id),
                        get_post_features(candidate_ids),
                    )
                    span.set_attribute("stage3.feature_source", "redis-fallback")

            # Attach post features to each candidate dict
            for c in candidates:
                c["post_features"] = post_feature_map.get(c["post_id"], {})

            ranked = await ranking_client.score_candidates(user_features, candidates)

        # ═══════════════════════════════════════════════════════════════
        #  STAGE 4 — Re-ranking, Business Rules & Hydration
        # ═══════════════════════════════════════════════════════════════
        with tracer.start_as_current_span("stage4_rerank_hydrate"):
            # Business rule: max MAX_AUTHOR_POSTS posts per author
            # (requires knowing author per post — fetched from TiDB below)
            top_ids = [c["post_id"] for c in ranked[: settings.feed_page_size * 3]]

            # Bulk fetch posts from TiDB
            rows = await db.execute(select(Post).where(Post.post_id.in_(top_ids)))
            post_map: dict[str, Post] = {p.post_id: p for p in rows.scalars().all()}

            # Build score lookup
            score_map = {c["post_id"]: c.get("rank_score", 0.0) for c in ranked}
            source_map = {c["post_id"]: c.get("source", "social") for c in candidates}

            # Apply author diversity cap
            author_post_count: dict[str, int] = defaultdict(int)
            feed_posts: list[FeedPost] = []
            served_ids: list[str] = []

            for post_id in top_ids:
                if len(feed_posts) >= settings.feed_page_size:
                    break
                post = post_map.get(post_id)
                if not post:
                    continue
                if author_post_count[post.user_id] >= MAX_AUTHOR_POSTS:
                    continue

                author_post_count[post.user_id] += 1
                served_ids.append(post_id)
                feed_posts.append(
                    FeedPost(
                        post_id=post.post_id,
                        user_id=post.user_id,
                        username=post.author.username if post.author else None,
                        display_name=post.author.display_name if post.author else None,
                        content=post.content,
                        media_url=get_presigned_url(post.media_key) if post.media_key else None,
                        media_type=post.media_type,
                        like_count=post.like_count,
                        created_at=post.created_at,
                        rank_score=score_map.get(post_id, 0.0),
                        source=source_map.get(post_id, "social"),
                    )
                )

        # Emit impressions asynchronously (fire-and-forget)
        if served_ids:
            import asyncio
            asyncio.create_task(publish_impressions(user_id, served_ids))

        latency_ms = (time.time() - start_time) * 1000
        FEED_LATENCY.observe(latency_ms / 1000)
        span.set_attribute("feed.latency_ms", latency_ms)
        span.set_attribute("feed.posts_returned", len(feed_posts))

        return FeedResponse(
            user_id=user_id,
            posts=feed_posts,
            candidates_social=len(social_ids),
            candidates_discovery=len(discovery_ids),
            candidates_after_filter=after_filter,
            latency_ms=round(latency_ms, 2),
        )


@router.post("/impressions", status_code=204)
async def record_impressions(user_id: str, post_ids: list[str]):
    """
    Manually record that a user saw specific posts.
    Typically called by the client after rendering the feed.
    """
    await publish_impressions(user_id, post_ids)
