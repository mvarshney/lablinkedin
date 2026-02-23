"""
MinIO (S3-compatible) client for media storage.

Stores actual image/video bytes as objects.
Returns pre-signed URLs so the client can stream media directly from MinIO
without going through the API service (saves bandwidth).
"""
import base64
import logging
import uuid
from io import BytesIO
from typing import Optional

import boto3
from botocore.client import Config

from app.config import settings

logger = logging.getLogger(__name__)

_s3 = None


def init_minio() -> None:
    """Create the S3 client and ensure the media bucket exists."""
    global _s3
    _s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )

    # Create bucket if missing
    existing = [b["Name"] for b in _s3.list_buckets().get("Buckets", [])]
    if settings.minio_bucket not in existing:
        _s3.create_bucket(Bucket=settings.minio_bucket)
        logger.info("Created MinIO bucket '%s'", settings.minio_bucket)
    else:
        logger.info("MinIO bucket '%s' already exists", settings.minio_bucket)


def get_s3():
    if _s3 is None:
        raise RuntimeError("MinIO client not initialised — call init_minio() at startup")
    return _s3


def upload_media(media_base64: str, media_type: str) -> str:
    """
    Decode base64 media, upload to MinIO, return the object key.
    Key format: {media_type}/{uuid}.{ext}
    """
    ext = "jpg" if media_type == "image" else "mp4"
    key = f"{media_type}/{uuid.uuid4()}.{ext}"
    data = base64.b64decode(media_base64)
    content_type = "image/jpeg" if media_type == "image" else "video/mp4"

    s3 = get_s3()
    s3.put_object(
        Bucket=settings.minio_bucket,
        Key=key,
        Body=BytesIO(data),
        ContentType=content_type,
    )
    logger.debug("Uploaded media to MinIO: %s", key)
    return key


def get_presigned_url(media_key: str, expires_in: int = 3600) -> Optional[str]:
    """Generate a temporary pre-signed URL valid for `expires_in` seconds."""
    if not media_key:
        return None
    s3 = get_s3()
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.minio_bucket, "Key": media_key},
            ExpiresIn=expires_in,
        )
        return url
    except Exception as exc:
        logger.warning("Failed to generate presigned URL for %s: %s", media_key, exc)
        return None
