"""
Configuration management using Pydantic BaseSettings.
All values can be overridden via environment variables or a .env file.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── TiDB (MySQL-protocol compatible) ───────────────────────────────────
    tidb_host: str = "tidb"
    tidb_port: int = 4000
    tidb_user: str = "root"
    tidb_password: str = ""
    tidb_database: str = "social_feed"

    @property
    def tidb_url(self) -> str:
        return (
            f"mysql+aiomysql://{self.tidb_user}:{self.tidb_password}"
            f"@{self.tidb_host}:{self.tidb_port}/{self.tidb_database}"
        )

    # ── Redis ──────────────────────────────────────────────────────────────
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_feed_ttl: int = 86400          # 24h TTL for feed mailboxes
    redis_feed_max_size: int = 500       # max items per user mailbox

    # ── Kafka ──────────────────────────────────────────────────────────────
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_topic_new_posts: str = "new-posts"
    kafka_topic_impressions: str = "impressions"

    # ── Qdrant ─────────────────────────────────────────────────────────────
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333
    qdrant_collection: str = "posts"
    embedding_dimension: int = 384       # all-MiniLM-L6-v2 output dim

    # ── MinIO (S3-compatible) ──────────────────────────────────────────────
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "media"
    minio_use_ssl: bool = False

    # ── Apache Pinot ───────────────────────────────────────────────────────
    pinot_broker_host: str = "pinot-broker"
    pinot_broker_port: int = 8099
    pinot_impressions_table: str = "impressions"
    pinot_lookback_hours: int = 24

    # ── Feast Feature Store ────────────────────────────────────────────────────
    feast_server_url: str = "http://feast-feature-store:6566"

    # ── Ranking Service ────────────────────────────────────────────────────
    ranking_service_url: str = "http://ranking-service:8001"
    ranking_candidate_limit: int = 100   # candidates per source
    feed_page_size: int = 20             # final posts returned

    # ── Observability ──────────────────────────────────────────────────────
    otel_exporter_otlp_endpoint: str = "http://jaeger:4317"
    service_name: str = "api-service"
    environment: str = "development"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
