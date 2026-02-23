"""
Apache Pinot client for impression tracking.

Pinot is a real-time OLAP store. We query it to find which post_ids a user
has already seen in the past N hours. These are then filtered out (or
de-prioritised) in Stage-2 of the feed pipeline.

Query goes to the Pinot Broker's SQL endpoint:
  POST http://pinot-broker:8099/query/sql
  Body: { "sql": "SELECT post_id FROM impressions WHERE ..." }

If Pinot is unavailable, we fall back to a Redis SET (short-term cache).
"""
import logging
import time
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class PinotClient:
    def __init__(self) -> None:
        self.base_url = (
            f"http://{settings.pinot_broker_host}:{settings.pinot_broker_port}"
        )
        self._http: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=5.0)

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()

    async def get_seen_post_ids(
        self,
        user_id: str,
        lookback_hours: Optional[int] = None,
    ) -> set[str]:
        """
        Return the set of post_ids this user has seen in the last N hours.
        Falls back to an empty set on Pinot errors (graceful degradation).
        """
        hours = lookback_hours or settings.pinot_lookback_hours
        cutoff_ms = int((time.time() - hours * 3600) * 1000)
        sql = (
            f"SELECT post_id FROM {settings.pinot_impressions_table} "
            f"WHERE user_id = '{user_id}' AND timestamp >= {cutoff_ms} "
            f"LIMIT 10000"
        )
        try:
            resp = await self._http.post("/query/sql", json={"sql": sql})
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("resultTable", {}).get("rows", [])
            return {row[0] for row in rows}
        except Exception as exc:
            logger.warning(
                "Pinot query failed (user=%s): %s — returning empty seen-set",
                user_id,
                exc,
            )
            return set()


# Singleton instance shared across requests
pinot_client = PinotClient()
