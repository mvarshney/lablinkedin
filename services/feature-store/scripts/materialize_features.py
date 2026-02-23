#!/usr/bin/env python3
"""
Feast offline-store population + online materialization.

Reads from TiDB to build three feature tables, writes them as Parquet files
into the Feast offline store, then runs 'feast materialize' to push the
latest snapshot into the Redis online store.

Run this:
  • Once after 'feast apply' (during container startup)
  • Again after seeding the DB: docker exec feast-feature-store python /feature-repo/scripts/materialize_features.py
  • Periodically (cron) in production to keep features fresh
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pymysql
import pymysql.cursors

# ── Config ─────────────────────────────────────────────────────────────────────
TIDB_HOST = os.environ.get("TIDB_HOST", "tidb")
TIDB_PORT = int(os.environ.get("TIDB_PORT", "3306"))
TIDB_DB   = os.environ.get("TIDB_DATABASE", "social_feed")
DATA_DIR  = "/feature-repo/data"
EMBED_DIM = 384

os.makedirs(DATA_DIR, exist_ok=True)
now = datetime.now(timezone.utc)

# ── Connect ─────────────────────────────────────────────────────────────────────
try:
    conn = pymysql.connect(
        host=TIDB_HOST,
        port=TIDB_PORT,
        user="root",
        database=TIDB_DB,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )
    print(f"Connected to TiDB at {TIDB_HOST}:{TIDB_PORT}/{TIDB_DB}")
except Exception as exc:
    print(f"Cannot connect to TiDB: {exc}. Skipping materialization.")
    sys.exit(0)

# ── 1. user_stats ───────────────────────────────────────────────────────────────
print("Building user_stats ...")
with conn.cursor() as cur:
    cur.execute("""
        SELECT
            u.user_id,
            COUNT(DISTINCT f1.follower_id)  AS follower_count,
            COUNT(DISTINCT f2.followee_id)  AS following_count,
            COUNT(DISTINCT p.post_id)       AS total_posts
        FROM users u
        LEFT JOIN follows f1 ON f1.followee_id = u.user_id
        LEFT JOIN follows f2 ON f2.follower_id = u.user_id
        LEFT JOIN posts   p  ON p.user_id = u.user_id
        GROUP BY u.user_id
    """)
    user_rows = cur.fetchall()

if not user_rows:
    print("  No users found — skipping user_stats")
else:
    user_df = pd.DataFrame(user_rows)
    user_df["event_timestamp"] = now
    # avg_engagement_rate: synthesised from follower/post ratio for the lab.
    # In production this comes from a streaming engagement aggregation job.
    user_df["avg_engagement_rate"] = (
        np.random.uniform(0.01, 0.15, len(user_df)).astype("float32")
    )
    # Interest vector: placeholder random vectors.
    # In production these are maintained by the embedding-worker as the
    # user likes / watches content (exponential moving average of post embeddings).
    user_df["interest_vector_json"] = [
        json.dumps(np.random.uniform(-1, 1, EMBED_DIM).tolist())
        for _ in range(len(user_df))
    ]
    out = f"{DATA_DIR}/user_stats.parquet"
    user_df.to_parquet(out, index=False)
    print(f"  Wrote {len(user_df)} rows → {out}")

# ── 2. post_stats ───────────────────────────────────────────────────────────────
print("Building post_stats ...")
with conn.cursor() as cur:
    cur.execute("""
        SELECT
            p.post_id,
            p.user_id                                       AS author_id,
            p.like_count,
            CASE WHEN p.media_key IS NOT NULL THEN 1 ELSE 0 END AS has_media,
            LENGTH(p.content)                               AS content_length,
            COUNT(DISTINCT f.follower_id)                   AS author_follower_count
        FROM posts p
        LEFT JOIN follows f ON f.followee_id = p.user_id
        GROUP BY p.post_id, p.user_id, p.like_count, p.media_key, p.content
    """)
    post_rows = cur.fetchall()

if not post_rows:
    print("  No posts found — skipping post_stats")
else:
    post_df = pd.DataFrame(post_rows)
    post_df["event_timestamp"] = now
    post_df["comment_count"]   = 0        # no comment table yet
    post_df["like_count"]      = post_df["like_count"].astype(int)
    post_df["has_media"]       = post_df["has_media"].astype(int)
    post_df["content_length"]  = post_df["content_length"].astype(int)
    post_df["author_follower_count"] = post_df["author_follower_count"].astype(int)
    # Post embeddings: placeholder random vectors.
    # In production these come from the embedding-worker which writes to Qdrant.
    # For Feast we store a copy here so the feature server can serve them
    # for topic_similarity computation.
    post_df["embedding_json"] = [
        json.dumps(np.random.uniform(-1, 1, EMBED_DIM).tolist())
        for _ in range(len(post_df))
    ]
    out = f"{DATA_DIR}/post_stats.parquet"
    post_df.to_parquet(out, index=False)
    print(f"  Wrote {len(post_df)} rows → {out}")

# ── 3. user_author_affinity ─────────────────────────────────────────────────────
print("Building user_author_affinity ...")
with conn.cursor() as cur:
    cur.execute("""
        SELECT
            l.user_id,
            p.user_id AS author_id,
            COUNT(*)  AS interaction_count
        FROM likes l
        JOIN posts p ON p.post_id = l.post_id
        GROUP BY l.user_id, p.user_id
    """)
    affinity_rows = cur.fetchall()

if not affinity_rows:
    print("  No likes found — skipping user_author_affinity")
else:
    aff_df = pd.DataFrame(affinity_rows)
    aff_df["event_timestamp"] = now
    max_ic = aff_df["interaction_count"].max() or 1
    aff_df["affinity_score"] = (aff_df["interaction_count"] / max_ic).astype("float32")
    out = f"{DATA_DIR}/user_author_affinity.parquet"
    aff_df.to_parquet(out, index=False)
    print(f"  Wrote {len(aff_df)} rows → {out}")

conn.close()

# ── 4. feast materialize ────────────────────────────────────────────────────────
# Push the Parquet snapshots into the Redis online store.
# Window: last 24 h → now+1 min (covers freshly written event_timestamp = now)
start_ts = (now - timedelta(hours=24)).isoformat()
end_ts   = (now + timedelta(minutes=1)).isoformat()

print(f"\nRunning: feast materialize {start_ts} {end_ts}")
result = subprocess.run(
    ["feast", "-c", "/feature-repo", "materialize", start_ts, end_ts],
    capture_output=True,
    text=True,
)
print(result.stdout)
if result.returncode != 0:
    print("Materialization warning:", result.stderr, file=sys.stderr)
    sys.exit(1)

print("Feature materialization complete.")
