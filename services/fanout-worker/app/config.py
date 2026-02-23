from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_topic_new_posts: str = "new-posts"
    kafka_consumer_group: str = "fanout-worker"

    # TiDB (MySQL-compatible) — social graph
    tidb_host: str = "tidb"
    tidb_port: int = 4000
    tidb_user: str = "root"
    tidb_password: str = ""
    tidb_database: str = "social_feed"

    @property
    def tidb_dsn(self) -> str:
        return (
            f"{self.tidb_user}:{self.tidb_password}"
            f"@{self.tidb_host}:{self.tidb_port}/{self.tidb_database}"
        )

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_feed_ttl: int = 86400
    redis_feed_max_size: int = 500

    # Fan-out cap — skip fan-out for authors with >N followers (celebrities)
    # In a real system these would be served differently (pull-on-read)
    fan_out_follower_cap: int = 10_000

    # OTel
    otel_exporter_otlp_endpoint: str = "http://jaeger:4317"
    service_name: str = "fanout-worker"

    class Config:
        env_file = ".env"


settings = Settings()
