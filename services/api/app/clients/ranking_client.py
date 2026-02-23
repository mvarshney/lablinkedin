"""
Ranking service client.

In production this would call NVIDIA Triton Inference Server running a
Deep FM or similar CTR model. For learning purposes we call a lightweight
mock FastAPI service that scores candidates based on engineered features.

If the ranking service is unavailable, we fall back to a simple heuristic
(recency + like_count) so the feed always returns results.
"""
import logging
import math
import time
from typing import Optional

import httpx

from app.config import settings
from app.telemetry import RANKING_ERRORS_TOTAL

logger = logging.getLogger(__name__)


class RankingClient:
    def __init__(self) -> None:
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.ranking_service_url, timeout=2.0
        )

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()

    async def score_candidates(
        self,
        user_features: dict,
        candidates: list[dict],
    ) -> list[dict]:
        """
        Send a batch of candidate posts to the ranking service.

        Request body:
          { "user_features": {...}, "candidates": [{ "post_id", "post_features" }] }

        Response:
          { "scores": [{ "post_id", "score" }] }

        Each candidate dict must contain: post_id + post_features dict.
        Returns candidates with an added 'rank_score' field, sorted desc.
        """
        if not candidates:
            return []

        payload = {
            "user_features": user_features,
            "candidates": candidates,
        }

        try:
            resp = await self._http.post("/rank", json=payload)
            resp.raise_for_status()
            scores: list[dict] = resp.json()["scores"]
            score_map = {s["post_id"]: s["score"] for s in scores}
            for c in candidates:
                c["rank_score"] = score_map.get(c["post_id"], 0.0)
        except Exception as exc:
            logger.warning(
                "Ranking service unavailable: %s — using heuristic fallback", exc
            )
            RANKING_ERRORS_TOTAL.inc()
            _apply_heuristic_scores(candidates)

        candidates.sort(key=lambda c: c["rank_score"], reverse=True)
        return candidates


def _apply_heuristic_scores(candidates: list[dict]) -> None:
    """
    Fallback scoring when the ML service is down.
    score = 0.5 * recency_score + 0.5 * normalised_likes
    """
    now = time.time()
    max_likes = max((c.get("post_features", {}).get("like_count", 0) for c in candidates), default=1) or 1

    for c in candidates:
        pf = c.get("post_features", {})
        age_hours = (now - pf.get("created_at_ts", now)) / 3600
        recency = math.exp(-age_hours / 48)  # decay with 48h half-life
        likes_norm = pf.get("like_count", 0) / max_likes
        c["rank_score"] = round(0.5 * recency + 0.5 * likes_norm, 4)


# Singleton
ranking_client = RankingClient()
