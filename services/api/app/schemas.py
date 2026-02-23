"""
Pydantic request / response schemas for the API layer.
Kept separate from ORM models to avoid coupling transport to storage.
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ──────────────────────────── Users ───────────────────────────────────────

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    display_name: Optional[str] = None


class UserResponse(BaseModel):
    user_id: str
    username: str
    display_name: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class FollowRequest(BaseModel):
    follower_id: str
    followee_id: str


# ──────────────────────────── Posts ───────────────────────────────────────

class PostCreate(BaseModel):
    user_id: str
    content: Optional[str] = None
    # Base64-encoded media payload — stored in MinIO; optional
    media_base64: Optional[str] = None
    media_type: Optional[str] = Field(None, pattern="^(image|video)$")


class PostResponse(BaseModel):
    post_id: str
    user_id: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    content: Optional[str]
    media_url: Optional[str]   # pre-signed MinIO URL
    media_type: Optional[str]
    like_count: int
    created_at: datetime

    class Config:
        from_attributes = True


class LikeRequest(BaseModel):
    user_id: str
    post_id: str


# ──────────────────────────── Feed ────────────────────────────────────────

class FeedPost(BaseModel):
    """A hydrated, ranked post returned in the feed."""
    post_id: str
    user_id: str
    username: Optional[str]
    display_name: Optional[str]
    content: Optional[str]
    media_url: Optional[str]
    media_type: Optional[str]
    like_count: int
    created_at: datetime
    # Ranking signals exposed for debugging / learning
    rank_score: float
    source: str   # 'social' | 'discovery'


class FeedResponse(BaseModel):
    user_id: str
    posts: list[FeedPost]
    # Metadata useful for understanding the pipeline
    candidates_social: int
    candidates_discovery: int
    candidates_after_filter: int
    latency_ms: float


# ──────────────────────────── Impressions ─────────────────────────────────

class ImpressionRecord(BaseModel):
    user_id: str
    post_ids: list[str]
