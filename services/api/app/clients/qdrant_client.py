"""
Qdrant vector database client.

Collection layout:
  name    : posts  (settings.qdrant_collection)
  vector  : 384-dim float (all-MiniLM-L6-v2)
  payload : { user_id, created_at_ts, like_count, content_type }

Used for the Discovery (out-of-network) retrieval stage.
"""
import logging
from typing import Optional

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from app.config import settings

logger = logging.getLogger(__name__)

_qdrant: Optional[AsyncQdrantClient] = None


async def init_qdrant() -> None:
    global _qdrant
    _qdrant = AsyncQdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
    )

    # Create collection if it doesn't exist
    existing = await _qdrant.get_collections()
    names = [c.name for c in existing.collections]
    if settings.qdrant_collection not in names:
        await _qdrant.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(
                size=settings.embedding_dimension,
                distance=Distance.COSINE,
            ),
        )
        logger.info("Created Qdrant collection '%s'", settings.qdrant_collection)
    else:
        logger.info("Qdrant collection '%s' already exists", settings.qdrant_collection)


def get_qdrant() -> AsyncQdrantClient:
    if _qdrant is None:
        raise RuntimeError("Qdrant not initialised — call init_qdrant() at startup")
    return _qdrant


async def upsert_post_vector(
    post_id: str,
    vector: list[float],
    payload: dict,
) -> None:
    """Store/update a post embedding in Qdrant."""
    client = get_qdrant()
    # Qdrant IDs must be unsigned ints or UUID strings
    await client.upsert(
        collection_name=settings.qdrant_collection,
        points=[
            PointStruct(
                id=post_id,
                vector=vector,
                payload=payload,
            )
        ],
    )


async def search_similar_posts(
    user_vector: list[float],
    limit: int = 100,
    exclude_user_id: Optional[str] = None,
) -> list[str]:
    """
    ANN search for posts similar to the user's interest vector.
    Returns a list of post_ids ordered by cosine similarity.
    """
    client = get_qdrant()

    # Optional filter: exclude the requesting user's own posts from discovery
    query_filter = None
    if exclude_user_id:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        query_filter = Filter(
            must_not=[
                FieldCondition(
                    key="user_id",
                    match=MatchValue(value=exclude_user_id),
                )
            ]
        )

    results = await client.search(
        collection_name=settings.qdrant_collection,
        query_vector=user_vector,
        limit=limit,
        query_filter=query_filter,
        with_payload=False,
    )
    return [str(r.id) for r in results]
