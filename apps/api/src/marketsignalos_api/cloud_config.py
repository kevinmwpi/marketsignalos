"""Validate the single-instance Railway deployment before starting ingestion."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def validate_cloud_config() -> None:
    if os.getenv("APP_ENV") != "production" and not os.getenv("RAILWAY_ENVIRONMENT_ID"):
        return
    if len(os.getenv("ADMIN_API_TOKEN", "").strip()) < 32:
        raise RuntimeError("Production requires ADMIN_API_TOKEN with at least 32 characters")
    configured = os.getenv("POLYMARKET_DATA_DIR", "")
    path = Path(configured)
    if not configured or not path.is_absolute():
        raise RuntimeError("Production requires an absolute POLYMARKET_DATA_DIR")
    if os.getenv("RAILWAY_ENVIRONMENT_ID"):
        mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
        if not mount or not path.resolve().is_relative_to(Path(mount).resolve()):
            raise RuntimeError("POLYMARKET_DATA_DIR must be inside the Railway persistent volume")
    if os.getenv("WEB_CONCURRENCY", "1") != "1":
        raise RuntimeError("JSONL ingestion requires WEB_CONCURRENCY=1 and one service replica")
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=path) as handle:
        handle.write(b"storage-probe")
        handle.flush()


if __name__ == "__main__":
    validate_cloud_config()
