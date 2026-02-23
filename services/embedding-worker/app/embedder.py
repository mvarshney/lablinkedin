"""
Embedding model wrapper.

Uses sentence-transformers (all-MiniLM-L6-v2, 384-dim).
The model is downloaded on first run and cached in /root/.cache/huggingface.
For even lower overhead you can swap this for an OpenAI/Cohere API call.
"""
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

_model = None


def load_model(model_name: str) -> None:
    """Load the sentence-transformers model into memory (done once at startup)."""
    global _model
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(model_name)
        logger.info("Loaded embedding model '%s'", model_name)
    except Exception as exc:
        logger.warning(
            "Could not load sentence-transformers model (%s) — "
            "falling back to random vectors. This is fine for local dev.",
            exc,
        )
        _model = None


def embed_text(text: str, dimension: int = 384) -> list[float]:
    """
    Return a normalised embedding for `text`.
    Falls back to a random unit vector if the model isn't available.
    """
    if _model is not None:
        try:
            vec = _model.encode(text, normalize_embeddings=True).tolist()
            return vec
        except Exception as exc:
            logger.warning("Embedding failed: %s — using random vector", exc)

    # Fallback: random unit vector
    import math
    raw = [random.gauss(0, 1) for _ in range(dimension)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]
