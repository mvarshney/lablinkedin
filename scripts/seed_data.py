#!/usr/bin/env python3
"""
Seed script — creates a realistic dataset for testing the feed system.

Creates:
  • 10 users
  • A follow graph (each user follows 3-5 others)
  • 5 posts per user (50 total)
  • Some likes across posts

Run after docker compose up:
  python scripts/seed_data.py --api-url http://localhost:8000

All IDs are printed so you can use them in curl commands.
"""
import argparse
import json
import random
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional


BASE_USERS = [
    ("alice_ai", "Alice Chen"),
    ("bob_builder", "Bob Martinez"),
    ("carol_codes", "Carol Singh"),
    ("dave_designs", "Dave Kim"),
    ("eve_engineer", "Eve Johnson"),
    ("frank_feeds", "Frank Williams"),
    ("grace_graphs", "Grace Li"),
    ("henry_hpc", "Henry Brown"),
    ("iris_infra", "Iris Davis"),
    ("jack_ml", "Jack Wilson"),
]

SAMPLE_POSTS = [
    "Just shipped a new feature to production 🚀 Zero downtime deploys are beautiful.",
    "Deep dive into vector databases today. Qdrant's HNSW index is impressively fast.",
    "TIL: Redis ZSETs are O(log N) for writes and O(1) for range reads. Perfect for feed mailboxes.",
    "Apache Kafka consumer groups are a masterclass in distributed coordination.",
    "Building a recommendation system from scratch. The cold-start problem is real.",
    "Fan-out on write vs pull-on-read — the eternal debate in feed architecture.",
    "Just tried sentence-transformers for the first time. 384-dim embeddings in 15ms. 🤯",
    "Why Pinot? Real-time OLAP at sub-second latency. Perfect for impression deduplication.",
    "k3s is the lightest Kubernetes distribution I have ever run. Boots in 30 seconds.",
    "OpenTelemetry traces finally connected to Jaeger. The waterfall diagram is so satisfying.",
    "MinIO is remarkably S3-compatible. Switched with zero code changes.",
    "Distributed SQL with TiDB — horizontal scaling without changing your SQL dialect.",
    "The beauty of a well-designed content feed: you never feel like you are searching.",
    "Content moderation at scale is a harder problem than the ranking model.",
    "Learned about approximate nearest neighbor (ANN) search today. HNSW > brute force.",
    "Social graph traversal at LinkedIn scale: 900M nodes, 50B edges. Mind-blowing.",
    "Embedding-based retrieval changed everything for recommendation systems.",
    "A/B testing your ranking model: always ship with a control group.",
    "The click-through rate (pClick) prediction problem at its core is just logistic regression.",
    "FastAPI async endpoints are a joy. Concurrent DB + Redis + Kafka calls in parallel.",
    "Just deployed with zero downtime using a rolling update in k8s.",
    "Grafana dashboards are the first thing I build for any new service.",
    "Prometheus metrics: the difference between knowing and guessing in production.",
    "My Redis memory usage spiked 3x after forgetting to set TTLs on feed ZSETs. 🤦",
    "The feed latency histogram shows p99 at 120ms. Time to optimise Stage 3 ranking.",
]


@dataclass
class ApiClient:
    base_url: str

    def post(self, path: str, data: dict) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"  HTTP {e.code} on POST {path}: {body}")
            return {}

    def get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} on GET {path}")
            return {}


def wait_for_api(client: ApiClient, retries: int = 15) -> None:
    print(f"Waiting for API at {client.base_url} ...")
    for i in range(retries):
        try:
            result = client.get("/health")
            if result.get("status") == "ok":
                print("  API is ready!\n")
                return
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError(f"API not reachable at {client.base_url} after {retries} retries")


def main(api_url: str) -> None:
    client = ApiClient(api_url)
    wait_for_api(client)

    # ── Create users ─────────────────────────────────────────────────────
    print("Creating users...")
    user_ids: list[str] = []
    for username, display_name in BASE_USERS:
        result = client.post("/users/", {"username": username, "display_name": display_name})
        uid = result.get("user_id", "")
        if uid:
            user_ids.append(uid)
            print(f"  ✓ {username} ({uid})")
        else:
            print(f"  ✗ Failed to create {username}")

    if not user_ids:
        print("No users created — aborting")
        return

    # ── Create follow graph ───────────────────────────────────────────────
    print("\nCreating follow relationships...")
    for follower_id in user_ids:
        # Each user follows 3-5 random others (not themselves)
        followees = random.sample([u for u in user_ids if u != follower_id], k=min(4, len(user_ids) - 1))
        for followee_id in followees:
            client.post("/users/follow", {"follower_id": follower_id, "followee_id": followee_id})
    print(f"  ✓ Follow graph created")

    # ── Create posts ──────────────────────────────────────────────────────
    print("\nCreating posts...")
    post_ids: list[str] = []
    random.shuffle(SAMPLE_POSTS)
    post_pool = SAMPLE_POSTS * 3  # repeat to have enough
    idx = 0
    for user_id in user_ids:
        for _ in range(5):
            content = post_pool[idx % len(post_pool)]
            idx += 1
            result = client.post("/posts/", {"user_id": user_id, "content": content})
            pid = result.get("post_id", "")
            if pid:
                post_ids.append(pid)
    print(f"  ✓ {len(post_ids)} posts created")

    # ── Create some likes ─────────────────────────────────────────────────
    print("\nAdding likes...")
    likes = 0
    for post_id in post_ids:
        # Each post gets 0-5 random likes
        for user_id in random.sample(user_ids, k=random.randint(0, 5)):
            result = client.post(f"/posts/{post_id}/like", {"user_id": user_id, "post_id": post_id})
            likes += 1
    print(f"  ✓ {likes} likes added")

    # ── Print summary ─────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Seed complete! Here are some commands to try:\n")
    u = user_ids[0]
    print(f"# Get the feed for user '{BASE_USERS[0][0]}':")
    print(f"  curl -s '{api_url}/feed/?user_id={u}' | python3 -m json.tool\n")
    print(f"# Create a new post:")
    print(f"  curl -s -X POST '{api_url}/posts/' \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"user_id\": \"{u}\", \"content\": \"Hello world!\"}}' | python3 -m json.tool\n")
    print(f"# Check Jaeger traces: http://localhost:16686")
    print(f"# Check Prometheus: http://localhost:9090")
    print(f"# Check Grafana: http://localhost:3000 (admin/admin)")
    print(f"# Check MinIO: http://localhost:9001 (minioadmin/minioadmin)")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the Social Feed system")
    parser.add_argument("--api-url", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()
    main(args.api_url)
