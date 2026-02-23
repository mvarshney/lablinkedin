# Instructions: Modern Content Feed System

## (a) Project Goal
The objective of this project is to build a high-scale, modern **content feed system** that mirrors the architecture of platforms like Instagram, TikTok and LinkedIn.

Unlike traditional "social-only" feeds, this project implements a **Hybrid Discovery Architecture**:
1.  **Social Graph:** Delivering posts from people a user explicitly follows (Fan-out on write).
2.  **Discovery Engine:** Surfacing "Out-of-Network" content using AI-driven vector similarity (Nearest Neighbor search).
3.  **Tiered Ranking:** A multi-stage pipeline that retrieves candidates, filters seen content, and uses machine learning to score and re-rank the final feed.

---

## (b) Software Stack
We are using a "Cloud-Native AI" stack designed for high throughput and low-latency retrieval:

| Component | Technology | Role |
| :--- | :--- | :--- |
| **Orchestration** | `k3s` (Kubernetes) | Cluster management and service scaling. |
| **API Layer** | `Python + FastAPI` | Entry point for `/post` and `/feed` endpoints. |
| **Source of Truth** | `TiDB` (Distributed SQL) | Persistent storage for User profiles, Post metadata, and Likes. |
| **Messaging** | `Apache Kafka` | Event backbone for asynchronous fan-out and embedding generation. |
| **Vector Database** | `Qdrant` | Storing and searching high-dimensional content embeddings for discovery. |
| **Social Graph** | `Dgraph` (or TiDB) | Managing follow/follower relationships. |
| **Feed Store** | `Redis` (ZSETs) | High-speed "Mailboxes" for the Social (following) feed + Feast online store. |
| **Feature Store** | `Feast` | Serving pre-computed ranking features (user stats, post stats, affinity) to the ranking model. Offline store: Parquet files. Online store: Redis. |
| **State/Analytics** | `Apache Pinot` | Real-time tracking of "seen" events for Impression Discounting. |
| **ML Inference** | `NVIDIA Triton` | Serving the ranking models (Deep FM or similar). |
| **Media Storage** | `MinIO` (S3 API) | Object storage for actual image and video files. |

---

## (c) Architecture & Connectivity

### 1. Post Ingestion Path (Write)
* **User Action:** User calls `POST /post`.
* **Persistence:** `PostService` saves metadata to `TiDB` and uploads media to `MinIO`.
* **Event:** A `NewPost` event is emitted to `Kafka`.
* **Asynchronous Workers:**
    * **Embedding Worker:** Pulls the post content, calls an embedding model, and saves the vector into `Qdrant` with metadata tags.
    * **Fan-out Worker:** Queries `Dgraph` for the author's followers. For each follower, it pushes the `Post_ID` into their specific `Redis` ZSET (Mailbox).

### 2. Feed Retrieval Path (Read)
* **User Action:** User calls `GET /feed`.
* **Stage 1: Retrieval (Candidate Generation):**
    * **Social:** Fetch top 100 `Post_IDs` from the user's `Redis` mailbox.
    * **Discovery:** Query `Qdrant` for top 100 posts similar to the user's `Interest_Vector`.
* **Stage 2: Filtering (Impression Discounting):**
    * Query `Apache Pinot` for `Post_IDs` the user has seen in the last 24 hours.
    * Remove or heavily penalize these IDs from the candidate pool.
* **Stage 3: Scoring (Ranking):**
    * Fetch features from the `Feast Feature Store` (HTTP call to the Feast Feature Server):
        * **`user_stats`** — `follower_count`, `avg_engagement_rate`, `interest_vector_json` (keyed by `user_id`)
        * **`post_stats`** — `like_count`, `has_media`, `author_id`, `embedding_json` (keyed by `post_id`)
        * **`user_author_affinity`** — `affinity_score` (keyed by `user_id` + `author_id`)
    * Compute cross-features locally: `topic_similarity` = cosine(`interest_vector`, `embedding`).
    * Falls back to Redis feature cache if the Feast server is unavailable.
    * Send batch of ≤150 candidates with full features to `Triton Inference Server` (or mock RankingService).
    * Ranking model returns a probability score for engagement (e.g., `pClick`).
* **Stage 4: Re-ranking & Hydration:**
    * Apply business logic (e.g., ensure no more than 2 posts from the same author).
    * Hydrate the top 20 `Post_IDs` with full metadata from `TiDB` and CDN URLs from `MinIO`.
    * **Response:** Return the final JSON list to the client.

---

## Implementation Notes for Coding Agent
1.  **Containerization:** All services must have a `Dockerfile` and a corresponding `Helm` chart or `K8s` manifest.
2.  **Concurrency:** Use Python's `asyncio` in the FastAPI services to handle concurrent calls to Redis, Qdrant, and TiDB.
3.  **Observability:** Implement basic Prometheus metrics for "Feed Latency" and "Retrieval Recall." We are more interested in Traces, so make sure those are implemented correctly.
4.  **Mocking:** If a real ML model is too heavy for the initial build, provide a mock `RankingService` that returns randomized scores before integrating Triton.
5. Do not worry about CI/CD (such as ArgoCD). Simple yaml files that are manually applied to k8s is enough for this project.
