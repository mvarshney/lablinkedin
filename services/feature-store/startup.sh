#!/bin/bash
# Feast Feature Store — container startup script
#
# Execution order:
#   1. Wait for Redis (online store)
#   2. Wait for TiDB (offline data source)
#   3. feast apply      — register feature definitions in the registry
#   4. materialize      — read TiDB → parquet → Redis (skipped if DB is empty)
#   5. feast serve      — start the HTTP feature server on port 6566

set -euo pipefail

REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"
TIDB_HOST="${TIDB_HOST:-tidb}"
TIDB_PORT="${TIDB_PORT:-3306}"
TIDB_DATABASE="${TIDB_DATABASE:-social_feed}"

# ── Wait for Redis ─────────────────────────────────────────────────────────────
echo "[feast-startup] Waiting for Redis at ${REDIS_HOST}:${REDIS_PORT} ..."
python3 - <<PYEOF
import time, sys
import redis, os
r = redis.Redis(host="${REDIS_HOST}", port=${REDIS_PORT})
for i in range(30):
    try:
        r.ping()
        print("[feast-startup] Redis ready")
        sys.exit(0)
    except Exception:
        time.sleep(2)
print("[feast-startup] Redis not available after 60s", file=sys.stderr)
sys.exit(1)
PYEOF

# ── Wait for TiDB ──────────────────────────────────────────────────────────────
echo "[feast-startup] Waiting for TiDB at ${TIDB_HOST}:${TIDB_PORT} ..."
python3 - <<PYEOF
import time, sys, pymysql
for i in range(30):
    try:
        conn = pymysql.connect(
            host="${TIDB_HOST}", port=${TIDB_PORT},
            user="root", database="${TIDB_DATABASE}",
            connect_timeout=5,
        )
        conn.close()
        print("[feast-startup] TiDB ready")
        sys.exit(0)
    except Exception:
        time.sleep(3)
print("[feast-startup] TiDB not available after 90s", file=sys.stderr)
sys.exit(1)
PYEOF

# ── Patch feature_store.yaml with runtime Redis address ───────────────────────
# Replace the default connection_string with the actual env-var values.
sed -i "s|connection_string: \"redis:6379\"|connection_string: \"${REDIS_HOST}:${REDIS_PORT}\"|g" \
    /feature-repo/feature_store.yaml

# ── Create stub Parquet files if they don't exist ─────────────────────────────
# feast apply infers feature schema by reading the FileSource Parquet files.
# They must exist (even as empty stubs) before apply runs — the real data is
# written later by materialize_features.py after the DB has been seeded.
echo "[feast-startup] Ensuring Parquet stub files exist ..."
python3 - <<'PYEOF'
import os
import pandas as pd

DATA_DIR = "/feature-repo/data"
os.makedirs(DATA_DIR, exist_ok=True)

stubs = {
    "user_stats.parquet": {
        "user_id":               pd.Series([], dtype="str"),
        "follower_count":        pd.Series([], dtype="int64"),
        "following_count":       pd.Series([], dtype="int64"),
        "total_posts":           pd.Series([], dtype="int64"),
        "avg_engagement_rate":   pd.Series([], dtype="float32"),
        "interest_vector_json":  pd.Series([], dtype="str"),
        "event_timestamp":       pd.Series([], dtype="datetime64[us, UTC]"),
    },
    "post_stats.parquet": {
        "post_id":               pd.Series([], dtype="str"),
        "author_id":             pd.Series([], dtype="str"),
        "like_count":            pd.Series([], dtype="int64"),
        "comment_count":         pd.Series([], dtype="int64"),
        "has_media":             pd.Series([], dtype="int64"),
        "content_length":        pd.Series([], dtype="int64"),
        "author_follower_count": pd.Series([], dtype="int64"),
        "embedding_json":        pd.Series([], dtype="str"),
        "event_timestamp":       pd.Series([], dtype="datetime64[us, UTC]"),
    },
    "user_author_affinity.parquet": {
        "user_id":           pd.Series([], dtype="str"),
        "author_id":         pd.Series([], dtype="str"),
        "interaction_count": pd.Series([], dtype="int64"),
        "affinity_score":    pd.Series([], dtype="float32"),
        "event_timestamp":   pd.Series([], dtype="datetime64[us, UTC]"),
    },
}

for filename, columns in stubs.items():
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        pd.DataFrame(columns).to_parquet(path, index=False)
        print(f"  Created stub: {path}")
    else:
        print(f"  Already exists: {path}")
PYEOF

# ── feast apply ───────────────────────────────────────────────────────────────
echo "[feast-startup] Applying Feast feature definitions ..."
feast -c /feature-repo apply

# ── Initial materialization ────────────────────────────────────────────────────
echo "[feast-startup] Running initial feature materialization ..."
python3 /feature-repo/scripts/materialize_features.py \
    || echo "[feast-startup] Materialization skipped (DB likely empty — re-run after seeding)"

# ── Start Feast Feature Server ─────────────────────────────────────────────────
echo "[feast-startup] Starting Feast Feature Server on 0.0.0.0:6566 ..."
exec feast -c /feature-repo serve --host 0.0.0.0 --port 6566
