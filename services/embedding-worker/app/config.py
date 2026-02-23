from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_topic_new_posts: str = "new-posts"
    kafka_consumer_group: str = "embedding-worker"

    # Qdrant
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333
    qdrant_collection: str = "posts"
    embedding_dimension: int = 384

    # Embedding model — sentence-transformers model name
    # "all-MiniLM-L6-v2" is lightweight (80MB) and produces 384-dim vectors
    embedding_model: str = "all-MiniLM-L6-v2"

    # OTel
    otel_exporter_otlp_endpoint: str = "http://jaeger:4317"
    service_name: str = "embedding-worker"

    class Config:
        env_file = ".env"


settings = Settings()
