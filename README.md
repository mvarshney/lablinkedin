# Social Feed System — Learning Lab

A production-grade content feed system that mirrors the architecture of Instagram, TikTok, and LinkedIn. Built for hands-on learning — **not for production use**.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         WRITE PATH                                  │
│                                                                     │
│  POST /post ──► PostService ──► TiDB (metadata)                    │
│                              └──► MinIO (media bytes)              │
│                              └──► Kafka (new-posts topic)          │
│                                      │                              │
│                        ┌─────────────┴──────────────┐              │
│                        ▼                             ▼              │
│               EmbeddingWorker                  FanoutWorker        │
│            (sentence-transformers)         (reads TiDB follows)    │
│                        │                             │              │
│                        ▼                             ▼              │
│                    Qdrant                    Redis ZSETs            │
│                (post vectors)            (follower mailboxes)      │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                         READ PATH (GET /feed)                       │
│                                                                     │
│  Stage 1: Candidate Generation                                      │
│    Social    ── Redis ZSET mailbox     → top 100 post_ids           │
│    Discovery ── Qdrant ANN search      → top 100 post_ids           │
│                                                                     │
│  Stage 2: Impression Filtering                                      │
│    Pinot (OLAP) ── "seen in last 24h?" → remove duplicates          │
│                                                                     │
│  Stage 3: ML Ranking  ◄── Feast Feature Store ──────────────────── │
│    Feast Server fetches:                                            │
│      user_stats   (follower_count, avg_engagement_rate, …)          │
│      post_stats   (like_count, has_media, embedding_json, …)        │
│    App computes cross-features:                                     │
│      topic_similarity  = cosine(interest_vector, post_embedding)    │
│      affinity_score    = user→author historical engagement          │
│    → RankingService (mock Triton) → pClick scores                  │
│                                                                     │
│  Stage 4: Re-rank + Hydrate                                         │
│    Business rules (author diversity) + TiDB hydration              │
│    → top 20 posts as JSON                                           │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Map

| Component | Technology | Port | Purpose |
|---|---|---|---|
| API | FastAPI | 8000 | `/post`, `/feed`, `/users` endpoints |
| Ranking | FastAPI (mock) | 8001 | Scores candidates; replaces Triton |
| Database | MySQL 8 (TiDB-compatible) | 4000 | Users, posts, follows, likes |
| Feed Store | Redis 7 | 6379 | ZSET mailboxes + Feast online store |
| Messaging | Kafka | 9092 | `new-posts` + `impressions` events |
| Vector DB | Qdrant | 6333 | Post embeddings for discovery |
| Object Store | MinIO | 9000 | Media files (images/video) |
| OLAP | Apache Pinot | 8099 | Impression deduplication |
| **Feature Store** | **Feast** | **6566** | **Ranking features: user_stats, post_stats, affinity** |
| Tracing | Jaeger | 16686 | Distributed traces (OTel/OTLP) |
| Metrics | Prometheus | 9090 | Feed latency + recall metrics |
| Dashboards | Grafana | 3000 | Live dashboards |

---

## Prerequisites

- **Docker** ≥ 24 with Docker Compose V2
- **Python** 3.10+ (for seed script only)
- **kubectl** + **k3s** (optional — for Kubernetes deployment)

---

## Option A — Local Dev with Docker Compose (Recommended)

This is the fastest way to get everything running.

### Step 1 — Clone and configure

```bash
cd lablinkedin
cp .env.example .env    # already done; edit if needed
```

### Step 2 — Start infrastructure first

Start infrastructure services and wait for them to become healthy before starting the apps. This avoids startup race conditions.

```bash
# Start only infrastructure (DB, Kafka, Redis, etc.)
docker compose up -d tidb redis zookeeper kafka qdrant minio \
                     pinot-controller pinot-broker pinot-server \
                     jaeger prometheus grafana

# Watch until all infra is healthy (~2-3 minutes)
docker compose ps
```

### Step 3 — Set up Pinot (impression tracking)

```bash
# Wait for pinot-controller to be healthy, then:
./scripts/init-pinot.sh
```

### Step 4 — Start the Feast Feature Store

Feast needs Redis (online store) and TiDB (offline data source) to be healthy.
Its startup script runs `feast apply` + an initial materialization automatically.

```bash
docker compose up -d feast-feature-store

# Watch startup logs (apply + materialize + serve take ~60s)
docker compose logs -f feast-feature-store

# Confirm the feature server is up
curl http://localhost:6566/health
```

### Step 5 — Start application services

```bash
docker compose up -d api embedding-worker fanout-worker ranking-service

# Check logs
docker compose logs -f api
docker compose logs -f embedding-worker
docker compose logs -f fanout-worker
```

### Step 6 — Seed test data

```bash
# Creates 10 users, follow graph, 50 posts, and some likes
python3 scripts/seed_data.py --api-url http://localhost:8000
```

### Step 7 — Re-materialize features (after seeding)

The initial materialization ran against an empty database.
Re-run it now that TiDB has actual users, posts, and likes:

```bash
docker exec feast-feature-store \
    python /feature-repo/scripts/materialize_features.py
```

This reads TiDB → writes Parquet snapshots → pushes to Redis online store.
Future seeds or batch updates should re-run this command.

### Step 8 — Verify everything works

```bash
# Health check
curl http://localhost:8000/health

# API docs (interactive Swagger UI)
open http://localhost:8000/docs

# Feature server health
curl http://localhost:6566/health
```

---

## Option B — Kubernetes with k3s

Use this to experience the real k8s deployment flow.

### Step 1 — Install k3s

```bash
curl -sfL https://get.k3s.io | sh -
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
```

### Step 2 — Build and import Docker images into k3s

k3s has its own container runtime. You must import locally-built images.

```bash
# Build all service images
docker build -t social-feed/api:latest              services/api/
docker build -t social-feed/embedding-worker:latest services/embedding-worker/
docker build -t social-feed/fanout-worker:latest    services/fanout-worker/
docker build -t social-feed/ranking-service:latest  services/ranking-service/
docker build -t social-feed/feast-feature-store:latest services/feature-store/

# Import into k3s (bypasses registry requirement)
docker save social-feed/api:latest                  | sudo k3s ctr images import -
docker save social-feed/embedding-worker:latest     | sudo k3s ctr images import -
docker save social-feed/fanout-worker:latest        | sudo k3s ctr images import -
docker save social-feed/ranking-service:latest      | sudo k3s ctr images import -
docker save social-feed/feast-feature-store:latest  | sudo k3s ctr images import -
```

### Step 3 — Apply manifests in order

```bash
# 1. Namespace first
kubectl apply -f k8s/namespace.yaml

# 2. Infrastructure (order matters — Zookeeper before Kafka/Pinot)
kubectl apply -f k8s/infra/redis.yaml
kubectl apply -f k8s/infra/tidb.yaml
kubectl apply -f k8s/infra/kafka.yaml        # includes Zookeeper
kubectl apply -f k8s/infra/qdrant.yaml
kubectl apply -f k8s/infra/minio.yaml
kubectl apply -f k8s/infra/pinot.yaml
kubectl apply -f k8s/infra/observability.yaml

# 3. Wait for infrastructure to be ready
kubectl wait --for=condition=ready pod -l app=kafka -n social-feed --timeout=120s
kubectl wait --for=condition=ready pod -l app=tidb  -n social-feed --timeout=120s

# 4. Feature Store (must be running before API)
kubectl apply -f k8s/apps/feast.yaml
kubectl wait --for=condition=ready pod -l app=feast-feature-store \
    -n social-feed --timeout=180s

# 5. Application services
kubectl apply -f k8s/apps/api.yaml            # also creates the app-config ConfigMap
kubectl apply -f k8s/apps/ranking-service.yaml
kubectl apply -f k8s/apps/workers.yaml

# 6. Verify
kubectl get pods -n social-feed
kubectl get svc  -n social-feed
```

### Step 4 — Access services

```bash
# API (NodePort 30800)
curl http://$(kubectl get node -o jsonpath='{.items[0].status.addresses[0].address}'):30800/health

# Port-forward for UIs
kubectl port-forward svc/jaeger    -n social-feed 16686:16686 &
kubectl port-forward svc/grafana   -n social-feed 3000:3000   &
kubectl port-forward svc/prometheus -n social-feed 9090:9090  &
kubectl port-forward svc/minio     -n social-feed 9001:9001   &
kubectl port-forward svc/feast-feature-store -n social-feed 6566:6566 &
```

### Step 5 — Seed data (k8s)

```bash
# Port-forward the API
kubectl port-forward svc/api -n social-feed 8000:8000 &

python3 scripts/seed_data.py --api-url http://localhost:8000

# Re-materialize features after seeding
kubectl exec -n social-feed deploy/feast-feature-store -- \
    python /feature-repo/scripts/materialize_features.py
```

---

## Playing Around — API Walkthrough

All examples use the default `http://localhost:8000`.

### 1. Create users

```bash
curl -s -X POST http://localhost:8000/users/ \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "display_name": "Alice"}' | python3 -m json.tool

# Save the user_id
ALICE_ID="<user_id from response>"
BOB_ID="<create bob similarly>"
```

### 2. Follow a user

```bash
curl -s -X POST http://localhost:8000/users/follow \
  -H "Content-Type: application/json" \
  -d "{\"follower_id\": \"$BOB_ID\", \"followee_id\": \"$ALICE_ID\"}"
```

### 3. Create a post (triggers fan-out + embedding)

```bash
curl -s -X POST http://localhost:8000/posts/ \
  -H "Content-Type: application/json" \
  -d "{\"user_id\": \"$ALICE_ID\", \"content\": \"Hello from Alice! This post will appear in Bob's social feed.\"}" \
  | python3 -m json.tool
```

### 4. Get the feed (see the 4-stage pipeline in action)

```bash
curl -s "http://localhost:8000/feed/?user_id=$BOB_ID" | python3 -m json.tool
# Notice: candidates_social, candidates_discovery, candidates_after_filter, latency_ms
```

### 5. Like a post

```bash
POST_ID="<post_id from step 3>"
curl -s -X POST "http://localhost:8000/posts/$POST_ID/like" \
  -H "Content-Type: application/json" \
  -d "{\"user_id\": \"$BOB_ID\", \"post_id\": \"$POST_ID\"}"
```

### 6. Upload a post with media (base64 image)

```bash
# Encode a small image
IMG_B64=$(base64 < /path/to/image.jpg)
curl -s -X POST http://localhost:8000/posts/ \
  -H "Content-Type: application/json" \
  -d "{\"user_id\": \"$ALICE_ID\", \"content\": \"Photo post\", \"media_base64\": \"$IMG_B64\", \"media_type\": \"image\"}" \
  | python3 -m json.tool
# Response includes a pre-signed MinIO URL for the image
```

### 7. Query the Feast Feature Server directly

```bash
# Get user + post features for a ranking batch
curl -s -X POST http://localhost:6566/get-online-features \
  -H "Content-Type: application/json" \
  -d "{
    \"feature_service\": \"ranking_features\",
    \"entities\": {
      \"user_id\": [\"$ALICE_ID\", \"$ALICE_ID\"],
      \"post_id\": [\"$POST_ID\", \"<another_post_id>\"]
    }
  }" | python3 -m json.tool
```

### 8. Watch traces in Jaeger

```
Open: http://localhost:16686
Service: api-service
Operation: get_feed

You will see spans for:
  stage1_retrieval              — parallel Redis + Qdrant calls
  stage2_impression_filtering   — Pinot query
  stage3_feature_fetch          — Feast call (or Redis fallback)
    stage3.feature_source: feast | redis-fallback
  stage3_ranking                — ranking service call
  stage4_rerank_hydrate         — TiDB hydration
```

### 9. View metrics in Prometheus

```
Open: http://localhost:9090

Key metrics:
  feed_latency_seconds          — histogram of end-to-end feed latency
  retrieval_recall_ratio        — how much fresh content survives filtering
  feed_candidates_total         — social vs discovery candidate counts
  post_ingestion_total          — posts ingested over time
  ranking_errors_total          — ranking service fallback events
  ranking_latency_seconds       — time spent in the ranking service
```

### 10. Set up Grafana dashboard

```
1. Open http://localhost:3000 (admin / admin)
2. Prometheus datasource is pre-configured
3. Create a new dashboard → Add panel
4. Query: rate(feed_latency_seconds_sum[5m]) / rate(feed_latency_seconds_count[5m])
   Title: "Average Feed Latency"
5. Query: retrieval_recall_ratio
   Title: "Impression Recall (freshness)"
```

---

## Understanding the Feed Pipeline — Key Concepts

### Fan-out on Write vs Pull-on-Read

When Alice posts, the **fan-out worker** immediately pushes her `post_id` into every follower's Redis ZSET mailbox. This means Bob's feed read is O(1) — just `ZREVRANGE feed:{bob_id} 0 99`. The tradeoff: celebrities with millions of followers would flood Redis writes. We handle this with the **celebrity bypass** (see `fanout-worker/app/main.py:fan_out_follower_cap`).

### Interest Vector and Discovery

Every user has a 384-dimensional interest vector in Redis. Initially random (cold start), it should be updated by the embedding worker as the user likes/engages with content. The ANN search in Qdrant finds the 100 posts whose embeddings are most similar to this vector — this is how TikTok serves "out-of-network" content you've never seen before.

### Impression Deduplication

Once Bob sees a post, its `post_id` is sent to Kafka's `impressions` topic → Pinot ingests it in real-time. The next time Bob requests a feed, Stage 2 queries Pinot: `SELECT post_id WHERE user_id = bob AND timestamp > now - 24h`. Those IDs are filtered out, keeping the feed fresh.

### Feast Feature Store

The **Feast Feature Store** solves the **training-serving skew** problem: ensuring the ranking model sees the same features during training (offline) as during serving (online).

Three feature views are registered:

| Feature View | Entity | Key Features |
|---|---|---|
| `user_stats` | `user_id` | `follower_count`, `avg_engagement_rate`, `interest_vector_json` |
| `post_stats` | `post_id` | `like_count`, `has_media`, `author_follower_count`, `embedding_json` |
| `user_author_affinity` | `user_id` + `author_id` | `affinity_score`, `interaction_count` |

**Online store**: Redis (same instance, isolated key namespace under `feast_social_feed:*`)
**Offline store**: Parquet files (populated by `scripts/materialize_features.py`)

At **ranking time** (Stage 3), the API calls:
```
POST /get-online-features
{ "feature_service": "ranking_features",
  "entities": { "user_id": [uid]*n, "post_id": [p1..pn] } }
```
It also computes `topic_similarity` locally from the returned `interest_vector_json` and `embedding_json` (cosine similarity). The full feature set is sent to the ranking service, which uses `affinity_score` and `topic_similarity` in its scoring formula.

**Fallback**: if the Feast server is unreachable, the API falls back to the existing Redis feature cache (`uf:{user_id}`, `pf:{post_id}`). The ranking service degrades gracefully by using `avg_engagement_rate` as a proxy for affinity and 0.5 as a neutral topic similarity.

### Ranking

The mock ranking service uses Feast-enriched features:
```
pClick = 0.35 * recency
       + 0.25 * engagement        (likes + media_bonus)
       + 0.20 * affinity_score    (Feast: user→author history)
       + 0.15 * topic_similarity  (Feast: cosine of interest vs embedding)
       + 0.05 * jitter            (simulates model uncertainty)
```

In production, replace this with a Deep FM or DLRM model served by NVIDIA Triton. The interface is identical — POST `/rank` with user+post features, receive scores.

---

## Teardown

```bash
# Docker Compose
docker compose down -v    # -v removes volumes (deletes all data)

# k3s
kubectl delete namespace social-feed
```

---

## Project Structure

```
lablinkedin/
├── services/
│   ├── api/                  # FastAPI — main entry point
│   │   ├── app/
│   │   │   ├── main.py       # App setup, lifespan
│   │   │   ├── config.py     # All settings (env vars)
│   │   │   ├── database.py   # Async SQLAlchemy engine
│   │   │   ├── models.py     # ORM: User, Follow, Post, Like
│   │   │   ├── schemas.py    # Pydantic request/response
│   │   │   ├── telemetry.py  # OTel + Prometheus setup
│   │   │   ├── routers/
│   │   │   │   ├── users.py  # CRUD + follow
│   │   │   │   ├── posts.py  # Ingestion (write path)
│   │   │   │   └── feed.py   # 4-stage pipeline (read path)
│   │   │   └── clients/
│   │   │       ├── redis_client.py
│   │   │       ├── qdrant_client.py
│   │   │       ├── kafka_producer.py
│   │   │       ├── minio_client.py
│   │   │       ├── pinot_client.py
│   │   │       ├── ranking_client.py
│   │   │       └── feast_client.py   # Feast feature server client
│   ├── embedding-worker/     # Kafka consumer → Qdrant
│   ├── fanout-worker/        # Kafka consumer → Redis ZSETs
│   ├── ranking-service/      # Mock Triton / scoring service
│   └── feature-store/        # Feast Feature Store
│       ├── Dockerfile
│       ├── requirements.txt
│       ├── startup.sh             # feast apply + materialize + serve
│       ├── feature_store.yaml     # Feast project config
│       ├── entities.py            # user, post, author entities
│       ├── feature_views.py       # user_stats, post_stats, affinity
│       ├── on_demand_features.py  # topic_similarity ODFV
│       └── scripts/
│           └── materialize_features.py  # TiDB → parquet → Redis
├── k8s/
│   ├── namespace.yaml
│   ├── infra/                # Redis, Kafka, TiDB, Qdrant, MinIO, Pinot, OTel
│   └── apps/                 # API, workers, ranking-service, feast
├── scripts/
│   ├── init-db.sql           # Database schema
│   ├── seed_data.py          # Test data generator
│   └── init-pinot.sh         # Pinot schema + table setup
├── monitoring/
│   ├── prometheus.yml        # Scrape config
│   └── grafana/              # Grafana provisioning
├── docker-compose.yml        # Full local stack
├── .env.example              # Environment variable template
└── README.md
```
