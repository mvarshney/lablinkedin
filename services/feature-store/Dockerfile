FROM python:3.11-slim

# System deps: sed for startup.sh redis-address patch
RUN apt-get update && apt-get install -y --no-install-recommends \
        sed \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Feature repository ─────────────────────────────────────────────────────────
# The feature repo lives at /feature-repo/. Feast is invoked with -c /feature-repo.
# The /feature-repo/data/ subdirectory is mounted as a Docker volume so that
# the registry (registry.db) and Parquet snapshots survive restarts.
RUN mkdir -p /feature-repo/data /feature-repo/scripts

# Feast project config (must be at the root of the feature repo)
COPY feature_store.yaml /feature-repo/feature_store.yaml

# Feature definitions — feast apply discovers all .py files in /feature-repo/
COPY entities.py             /feature-repo/entities.py
COPY feature_views.py        /feature-repo/feature_views.py
COPY on_demand_features.py   /feature-repo/on_demand_features.py

# Materialization script
COPY scripts/materialize_features.py /feature-repo/scripts/materialize_features.py

# Startup orchestration
COPY startup.sh /startup.sh
RUN chmod +x /startup.sh

EXPOSE 6566

# Healthcheck: Feast feature server exposes GET /health
HEALTHCHECK --interval=15s --timeout=5s --retries=10 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:6566/health')" \
    || exit 1

CMD ["/startup.sh"]
