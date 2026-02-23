"""
Feast entity definitions for the social feed ranking model.

An Entity is the primary key that joins features to requests.

  user   — the viewer requesting a feed (join key: user_id)
  post   — a content post being ranked   (join key: post_id)
  author — the creator of a post         (join key: author_id)
           used in user_author_affinity lookups

Entity values are UUIDs (strings) matching the UUIDs in TiDB.
"""
from feast import Entity

user = Entity(
    name="user",
    join_keys=["user_id"],
    description="A user of the social feed platform",
)

post = Entity(
    name="post",
    join_keys=["post_id"],
    description="A content post on the platform",
)

author = Entity(
    name="author",
    join_keys=["author_id"],
    description="The author (user) who created a post — used for affinity lookups",
)
