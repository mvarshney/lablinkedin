"""
Post ingestion endpoints:
  POST /posts      — create and publish a post
  GET  /posts/{id} — fetch a single post
  POST /posts/{id}/like — like a post
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Like, Post, User
from app.schemas import LikeRequest, PostCreate, PostResponse
from app.clients.kafka_producer import publish_new_post
from app.clients.minio_client import get_presigned_url, upload_media
from app.clients.redis_client import set_post_features
from app.telemetry import POST_INGESTION_TOTAL
import time

logger = logging.getLogger(__name__)
router = APIRouter()
tracer = trace.get_tracer(__name__)


def _build_post_response(post: Post) -> PostResponse:
    media_url = get_presigned_url(post.media_key) if post.media_key else None
    username = post.author.username if post.author else None
    display_name = post.author.display_name if post.author else None
    return PostResponse(
        post_id=post.post_id,
        user_id=post.user_id,
        username=username,
        display_name=display_name,
        content=post.content,
        media_url=media_url,
        media_type=post.media_type,
        like_count=post.like_count,
        created_at=post.created_at,
    )


@router.post("/", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
async def create_post(body: PostCreate, db: AsyncSession = Depends(get_db)):
    """
    Post Ingestion Path (write side of the feed system):

    1. Validate the author exists.
    2. Persist metadata to TiDB.
    3. Upload media to MinIO (if provided).
    4. Emit a 'NewPost' Kafka event → triggers embedding-worker + fanout-worker.
    5. Seed post feature cache in Redis (for ranking).
    """
    with tracer.start_as_current_span("create_post") as span:
        # Validate author
        user = await db.get(User, body.user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Author not found")

        # Upload media to MinIO
        media_key = None
        if body.media_base64 and body.media_type:
            try:
                media_key = upload_media(body.media_base64, body.media_type)
            except Exception as exc:
                logger.warning("Media upload failed: %s", exc)

        post = Post(
            user_id=body.user_id,
            content=body.content,
            media_key=media_key,
            media_type=body.media_type,
        )
        db.add(post)
        await db.flush()     # materialise post_id
        await db.refresh(post)  # load server-generated fields (created_at)

        span.set_attribute("post.id", post.post_id)
        span.set_attribute("post.user_id", post.user_id)

        # Seed feature cache — ranking service reads this during scoring
        await set_post_features(
            post.post_id,
            {
                "like_count": 0,
                "created_at_ts": time.time(),
                "content_length": len(body.content or ""),
                "has_media": int(media_key is not None),
            },
        )

        # Emit event — async workers handle fan-out & embedding
        await publish_new_post(
            post_id=post.post_id,
            user_id=post.user_id,
            content=post.content,
        )

        POST_INGESTION_TOTAL.inc()
        logger.info("Post created: %s by user %s", post.post_id, post.user_id)
        return _build_post_response(post)


@router.get("/{post_id}", response_model=PostResponse)
async def get_post(post_id: str, db: AsyncSession = Depends(get_db)):
    post = await db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return _build_post_response(post)


@router.post("/{post_id}/like", status_code=status.HTTP_204_NO_CONTENT)
async def like_post(post_id: str, body: LikeRequest, db: AsyncSession = Depends(get_db)):
    """Like a post — idempotent. Updates like_count in TiDB."""
    with tracer.start_as_current_span("like_post"):
        post = await db.get(Post, post_id)
        if not post:
            raise HTTPException(status_code=404, detail="Post not found")

        existing = await db.execute(
            select(Like).where(Like.user_id == body.user_id, Like.post_id == post_id)
        )
        if existing.scalar_one_or_none():
            return  # already liked

        db.add(Like(user_id=body.user_id, post_id=post_id))
        post.like_count += 1

        # Update cached post features
        await set_post_features(
            post_id,
            {
                "like_count": post.like_count,
                "created_at_ts": post.created_at.timestamp(),
                "content_length": len(post.content or ""),
                "has_media": int(post.media_key is not None),
            },
        )
