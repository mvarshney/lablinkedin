"""
Feast Feature Server client — used in Stage 3 (ML Ranking) of the feed pipeline.

The Feast feature server (port 6566) exposes a single endpoint:
  POST /get-online-features

Request format:
  {
    "feature_service": "ranking_features",
    "entities": {
      "user_id": [uid, uid, uid],   # repeated N times, one per candidate post
      "post_id": [p1,  p2,  p3 ]   # N candidate post IDs
    }
  }

Response (column-oriented):
  {
    "metadata": { "feature_names": ["user_id", "post_id", "user_stats__follower_count", ...] },
    "results":  [ { "values": [...], "statuses": [...] }, ... ]   # one entry per column
  }

Feature column names use the Feast convention: <feature_view>__<feature_name>.

This client:
  1. Calls /get-online-features with the ranking_features FeatureService.
  2. Parses the column-oriented response into per-post dicts.
  3. Calls /get-online-features for user_author_affinity (separate entity schema).
  4. Computes topic_similarity locally from interest_vector_json + embedding_json.
  5. Raises on any HTTP / connection error so the caller can fall back to Redis.
"""
import json
import logging
from typing import Optional

import httpx
import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

# Feast feature view prefixes (feature_view__feature_name convention)
_USER_PREFIX    = "user_stats__"
_POST_PREFIX    = "post_stats__"
_AFFINITY_PREFIX = "user_author_affinity__"


def _parse_vector(json_str: str | None) -> np.ndarray:
    """Decode a JSON-encoded float list; return empty array on failure."""
    try:
        return np.array(json.loads(json_str or "[]"), dtype=np.float32)
    except Exception:
        return np.array([], dtype=np.float32)


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [−1, 1]; returns 0.0 if either vector is empty."""
    if a.size == 0 or b.size == 0:
        return 0.0
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / norm) if norm > 0.0 else 0.0


def _parse_response(
    feature_names: list[str],
    results: list[dict],
    n_rows: int,
) -> list[dict]:
    """
    Convert Feast's column-oriented response into a list of row dicts.
    Each results[i] corresponds to feature_names[i] and has a .values list.
    """
    rows: list[dict] = [{} for _ in range(n_rows)]
    for col_idx, fname in enumerate(feature_names):
        values = results[col_idx].get("values", [])
        for row_idx in range(n_rows):
            val = values[row_idx] if row_idx < len(values) else None
            rows[row_idx][fname] = val
    return rows


class FeastClient:
    def __init__(self) -> None:
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.feast_server_url,
            timeout=1.5,
        )

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()

    async def get_ranking_features(
        self,
        user_id: str,
        candidate_ids: list[str],
    ) -> tuple[dict, dict[str, dict]]:
        """
        Fetch user and post features for the ranking model in one Feast call.

        Returns:
          user_features  — {feature_name: value}   (same for all candidates)
          post_features  — {post_id: {feature_name: value}}

        Cross-features added to each post's dict:
          topic_similarity — cosine(user interest_vector, post embedding)
        """
        if not candidate_ids:
            return {}, {}

        n = len(candidate_ids)
        payload = {
            "feature_service": "ranking_features",
            "entities": {
                "user_id": [user_id] * n,
                "post_id": candidate_ids,
            },
        }

        resp = await self._http.post("/get-online-features", json=payload)
        resp.raise_for_status()
        data = resp.json()

        feature_names: list[str] = data["metadata"]["feature_names"]
        results: list[dict] = data["results"]
        rows = _parse_response(feature_names, results, n)

        # ── Extract user features (broadcast — same value in every row) ───────
        user_features: dict = {}
        for fname in feature_names:
            if fname.startswith(_USER_PREFIX):
                key = fname[len(_USER_PREFIX):]
                user_features[key] = rows[0].get(fname) if rows else None

        # ── Extract per-post features ──────────────────────────────────────────
        post_features: dict[str, dict] = {}
        interest_vector = _parse_vector(user_features.get("interest_vector_json"))

        for row, pid in zip(rows, candidate_ids):
            pf: dict = {}
            for fname in feature_names:
                if fname.startswith(_POST_PREFIX):
                    key = fname[len(_POST_PREFIX):]
                    pf[key] = row.get(fname)

            # Compute topic_similarity from stored vectors
            post_embedding = _parse_vector(pf.get("embedding_json"))
            pf["topic_similarity"] = _cosine_sim(interest_vector, post_embedding)

            post_features[pid] = pf

        return user_features, post_features

    async def get_affinity_features(
        self,
        user_id: str,
        author_ids: list[str],
    ) -> dict[str, float]:
        """
        Fetch user→author affinity scores for a set of authors.

        Returns: {author_id: affinity_score}  (0.0 if not found)
        """
        if not author_ids:
            return {}

        unique = list(set(author_ids))
        n = len(unique)
        payload = {
            "features": ["user_author_affinity:affinity_score"],
            "entities": {
                "user_id":   [user_id] * n,
                "author_id": unique,
            },
        }

        try:
            resp = await self._http.post("/get-online-features", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("Affinity feature fetch failed: %s", exc)
            return {}

        feature_names: list[str] = data["metadata"]["feature_names"]
        results: list[dict] = data["results"]
        rows = _parse_response(feature_names, results, n)

        affinity_map: dict[str, float] = {}
        for row, aid in zip(rows, unique):
            score = row.get(f"{_AFFINITY_PREFIX}affinity_score") or \
                    row.get("affinity_score")
            affinity_map[aid] = float(score) if score is not None else 0.0
        return affinity_map


# Singleton — started/stopped in app lifespan (main.py)
feast_client = FeastClient()
