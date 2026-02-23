"""
User management endpoints:
  POST /users          — create a user profile
  GET  /users/{id}     — fetch a user profile
  POST /users/follow   — follow another user
  POST /users/unfollow — unfollow
  GET  /users/{id}/followers — list followers
"""
import logging
import random

from fastapi import APIRouter, Depends, HTTPException, status
from opentelemetry import trace
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Follow, User
from app.schemas import FollowRequest, UserCreate, UserResponse
from app.clients.redis_client import get_interest_vector, set_interest_vector, set_user_features
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()
tracer = trace.get_tracer(__name__)


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    Register a new user.

    Also initialises a random interest vector in Redis so the Discovery
    retrieval stage works immediately (before any real interaction data).
    In a real system this vector is updated continuously as the user engages.
    """
    with tracer.start_as_current_span("create_user"):
        existing = await db.execute(
            select(User).where(User.username == body.username)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Username '{body.username}' already taken",
            )

        user = User(username=body.username, display_name=body.display_name)
        db.add(user)
        await db.flush()  # get user_id before commit

        # Seed a random interest vector — gives the discovery engine something
        # to work with before the user has any interaction history.
        interest_vector = [random.uniform(-1, 1) for _ in range(settings.embedding_dimension)]
        await set_interest_vector(user.user_id, interest_vector)

        # Seed user feature cache for the ranking model
        await set_user_features(
            user.user_id,
            {"follow_count": 0, "post_count": 0, "avg_engagement": 0.0},
        )

        logger.info("Created user %s (id=%s)", user.username, user.user_id)
        return user


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: str, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/follow", status_code=status.HTTP_204_NO_CONTENT)
async def follow_user(body: FollowRequest, db: AsyncSession = Depends(get_db)):
    """
    Create a follower → followee edge in the social graph.

    The fan-out worker reads these edges from TiDB when distributing new posts
    to follower mailboxes in Redis.
    """
    with tracer.start_as_current_span("follow_user"):
        if body.follower_id == body.followee_id:
            raise HTTPException(status_code=400, detail="Cannot follow yourself")

        # Check both users exist
        for uid in (body.follower_id, body.followee_id):
            if not await db.get(User, uid):
                raise HTTPException(status_code=404, detail=f"User {uid} not found")

        existing = await db.execute(
            select(Follow).where(
                Follow.follower_id == body.follower_id,
                Follow.followee_id == body.followee_id,
            )
        )
        if existing.scalar_one_or_none():
            return  # already following — idempotent

        db.add(Follow(follower_id=body.follower_id, followee_id=body.followee_id))
        logger.info("%s followed %s", body.follower_id, body.followee_id)


@router.post("/unfollow", status_code=status.HTTP_204_NO_CONTENT)
async def unfollow_user(body: FollowRequest, db: AsyncSession = Depends(get_db)):
    with tracer.start_as_current_span("unfollow_user"):
        await db.execute(
            delete(Follow).where(
                Follow.follower_id == body.follower_id,
                Follow.followee_id == body.followee_id,
            )
        )


@router.get("/{user_id}/followers")
async def list_followers(user_id: str, db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(Follow.follower_id).where(Follow.followee_id == user_id)
    )
    return {"user_id": user_id, "followers": [r[0] for r in rows.all()]}
