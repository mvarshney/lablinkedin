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
import pyarrow as pa
import pyarrow.parquet as pq

DATA_DIR = "/feature-repo/data"
os.makedirs(DATA_DIR, exist_ok=True)

# Use explicit PyArrow schemas so column types are preserved with 0 rows.
# dtype="str" in pandas maps to object, which pyarrow stores as null in empty
# files — causing Feast's schema inference to raise ValueType.NULL errors.
stubs = {
    "user_stats.parquet": pa.schema([
        pa.field("user_id",              pa.string()),
        pa.field("follower_count",       pa.int64()),
        pa.field("following_count",      pa.int64()),
        pa.field("total_posts",          pa.int64()),
        pa.field("avg_engagement_rate",  pa.float32()),
        pa.field("interest_vector_json", pa.string()),
        pa.field("event_timestamp",      pa.timestamp("us", tz="UTC")),
    ]),
    "post_stats.parquet": pa.schema([
        pa.field("post_id",               pa.string()),
        pa.field("author_id",             pa.string()),
        pa.field("like_count",            pa.int64()),
        pa.field("comment_count",         pa.int64()),
        pa.field("has_media",             pa.int64()),
        pa.field("content_length",        pa.int64()),
        pa.field("author_follower_count", pa.int64()),
        pa.field("embedding_json",        pa.string()),
        pa.field("event_timestamp",       pa.timestamp("us", tz="UTC")),
    ]),
    "user_author_affinity.parquet": pa.schema([
        pa.field("user_id",          pa.string()),
        pa.field("author_id",        pa.string()),
        pa.field("interaction_count", pa.int64()),
        pa.field("affinity_score",   pa.float32()),
        pa.field("event_timestamp",  pa.timestamp("us", tz="UTC")),
    ]),
}

for filename, schema in stubs.items():
    path = os.path.join(DATA_DIR, filename)
    # Always recreate stubs so schema is correct even after bad previous runs.
    # materialize_features.py overwrites these with real data immediately after.
    table = pa.table({f.name: pa.array([], type=f.type) for f in schema}, schema=schema)
    pq.write_table(table, path)
    print(f"  Wrote stub: {path}")
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
