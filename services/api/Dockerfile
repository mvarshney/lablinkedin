FROM python:3.12-slim

WORKDIR /app

# Install system dependencies needed by aiomysql / cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

EXPOSE 8000

# Start with 2 Uvicorn workers — tune based on vCPU count in production
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
