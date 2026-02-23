"""
On-demand Feature View — topic_similarity

Computes the cosine similarity between the requesting user's interest vector
and a post's content embedding at serving time, without pre-materialising
every (user, post) pair into the online store.

How it works:
  1. The ranking pipeline requests the 'ranking_features' FeatureService,
     which fetches user_stats (incl. interest_vector_json) and post_stats
     (incl. embedding_json) from Redis.
  2. Feast runs this ODFV inline, passing those features as a DataFrame.
  3. The function decodes both JSON vectors and returns the cosine similarity.

Input column names follow Feast's convention: <feature_view>__<feature_name>
  user_stats__interest_vector_json
  post_stats__embedding_json

NOTE: In this lab the topic_similarity is also computed directly in
feast_client.py (API side) as a fallback. The ODFV is registered here to
demonstrate the pattern; it can be added to the FeatureService in feature_views.py
when the Feast server version supports cross-entity ODFVs stably.
"""
import json

import numpy as np
import pandas as pd
from feast import Field
from feast.on_demand_feature_view import on_demand_feature_view
from feast.types import Float32

from feature_views import post_stats, user_stats


def _cosine(iv_json: str, ev_json: str) -> float:
    try:
        iv = np.array(json.loads(iv_json or "[]"), dtype=np.float32)
        ev = np.array(json.loads(ev_json or "[]"), dtype=np.float32)
        if iv.size == 0 or ev.size == 0:
            return 0.0
        norm = np.linalg.norm(iv) * np.linalg.norm(ev)
        return float(np.dot(iv, ev) / norm) if norm > 0 else 0.0
    except Exception:
        return 0.0


@on_demand_feature_view(
    sources=[user_stats, post_stats],
    schema=[Field(name="topic_similarity", dtype=Float32)],
)
def topic_similarity(inputs: pd.DataFrame) -> pd.DataFrame:
    """
    Cosine similarity between the user's interest vector and the post's
    content embedding — a real-time cross feature for the ranking model.
    """
    result = pd.DataFrame()
    result["topic_similarity"] = inputs.apply(
        lambda row: _cosine(
            row.get("interest_vector_json", "[]"),
            row.get("embedding_json", "[]"),
        ),
        axis=1,
    ).astype("float32")
    return result
