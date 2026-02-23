"""
Feast Feature View definitions for the social feed ranking model.

Three feature views serve the ranking model (Stage 3 of the feed pipeline):

  user_stats            — per-user engagement & social graph stats
  post_stats            — per-post content & engagement signals
  user_author_affinity  — user's historical engagement with each author

Data sources are Parquet files written by scripts/materialize_features.py,
which reads from TiDB and writes feature snapshots.  Feast then materialises
those Parquet rows into the Redis online store so the feature server can
respond with sub-millisecond latency during ranking.

FeatureService 'ranking_features' bundles user_stats + post_stats into a
single get-online-features call:
  entities = { "user_id": [uid]*n, "post_id": [p1, p2, ...pn] }
The response contains one row per (user_id, post_id) pair, with user
features broadcast across all rows.
"""
from datetime import timedelta

from feast import FeatureService, FeatureView, Field, FileSource

from entities import author, post, user
from feast.types import Float32, Int64, String

# ── Offline data sources (Parquet files) ──────────────────────────────────────

user_stats_source = FileSource(
    path="/feature-repo/data/user_stats.parquet",
    timestamp_field="event_timestamp",
    description="TiDB-derived user statistics snapshot",
)

post_stats_source = FileSource(
    path="/feature-repo/data/post_stats.parquet",
    timestamp_field="event_timestamp",
    description="TiDB-derived post statistics snapshot",
)

user_author_affinity_source = FileSource(
    path="/feature-repo/data/user_author_affinity.parquet",
    timestamp_field="event_timestamp",
    description="Per-user per-author interaction counts derived from the likes table",
)

# ── Feature Views ──────────────────────────────────────────────────────────────

user_stats = FeatureView(
    name="user_stats",
    entities=[user],
    ttl=timedelta(hours=24),
    schema=[
        # Social graph
        Field(name="follower_count",      dtype=Int64),
        Field(name="following_count",     dtype=Int64),
        Field(name="total_posts",         dtype=Int64),
        # Engagement
        Field(name="avg_engagement_rate", dtype=Float32),
        # Interest vector for discovery / topic similarity (JSON-encoded list)
        Field(name="interest_vector_json", dtype=String),
    ],
    source=user_stats_source,
    description="User-level engagement and social graph statistics for ranking",
)

post_stats = FeatureView(
    name="post_stats",
    entities=[post],
    ttl=timedelta(hours=48),
    schema=[
        # Author identity (used to fetch affinity features)
        Field(name="author_id",             dtype=String),
        # Engagement signals
        Field(name="like_count",            dtype=Int64),
        Field(name="comment_count",         dtype=Int64),
        # Content signals
        Field(name="has_media",             dtype=Int64),   # 0 or 1
        Field(name="content_length",        dtype=Int64),
        # Author popularity
        Field(name="author_follower_count", dtype=Int64),
        # Content embedding for topic similarity (JSON-encoded 384-dim vector)
        Field(name="embedding_json",        dtype=String),
    ],
    source=post_stats_source,
    description="Post-level engagement metrics and content signals for ranking",
)

user_author_affinity = FeatureView(
    name="user_author_affinity",
    entities=[user, author],
    ttl=timedelta(days=7),
    schema=[
        # Normalised [0,1] affinity score: how much this user likes this author
        Field(name="affinity_score",     dtype=Float32),
        # Raw interaction count (likes on this author's posts)
        Field(name="interaction_count",  dtype=Int64),
    ],
    source=user_author_affinity_source,
    description="User's historical engagement affinity with a specific author",
)

# ── Feature Service ────────────────────────────────────────────────────────────
#
# A FeatureService groups features for a specific use-case.
# Requesting 'ranking_features' returns user + post features in one call,
# which the ranking service uses to compute pClick scores.

ranking_features = FeatureService(
    name="ranking_features",
    features=[
        user_stats[[
            "follower_count",
            "following_count",
            "avg_engagement_rate",
            "total_posts",
            "interest_vector_json",
        ]],
        post_stats[[
            "author_id",
            "like_count",
            "comment_count",
            "has_media",
            "content_length",
            "author_follower_count",
            "embedding_json",
        ]],
    ],
    description=(
        "Combined user and post features for the ML ranking model. "
        "Call with entity rows: {user_id: [uid]*n, post_id: [p1..pn]}."
    ),
)
