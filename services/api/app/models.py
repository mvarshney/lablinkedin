"""
SQLAlchemy ORM models for TiDB.

Tables:
  users   — user profiles + serialised interest vector
  follows — social graph edges (follower → followee)
  posts   — post metadata (media bytes stored in MinIO)
  likes   — user × post engagement
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    # Serialised list[float] — the user's averaged interest embedding.
    # Updated by the embedding-worker as the user interacts with content.
    interest_vector: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    posts = relationship("Post", back_populates="author", lazy="noload")


class Follow(Base):
    __tablename__ = "follows"

    follower_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id"), primary_key=True
    )
    followee_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Fast lookup "who follows user X?" — used by fan-out worker
        Index("idx_followee", "followee_id"),
    )


class Post(Base):
    __tablename__ = "posts"

    post_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id"), nullable=False
    )
    content: Mapped[Optional[str]] = mapped_column(Text)
    # MinIO object key — construct CDN URL as minio_endpoint/bucket/media_key
    media_key: Mapped[Optional[str]] = mapped_column(String(500))
    media_type: Mapped[Optional[str]] = mapped_column(
        String(20)
    )  # 'image' | 'video' | None
    like_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    author = relationship("User", back_populates="posts", lazy="joined")

    __table_args__ = (
        Index("idx_posts_user", "user_id"),
        Index("idx_posts_created", "created_at"),
    )


class Like(Base):
    __tablename__ = "likes"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id"), primary_key=True
    )
    post_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("posts.post_id"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
