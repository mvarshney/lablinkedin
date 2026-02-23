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
