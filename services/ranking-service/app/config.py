from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8001
    service_name: str = "ranking-service"
    otel_exporter_otlp_endpoint: str = "http://jaeger:4317"

    class Config:
        env_file = ".env"


settings = Settings()
